#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCFN-DSL -> Minecraft .mcfunction Transpiler (clean, maintainable)

Design goals
- Easy to read & modify: clear sections (Lexer/Parser/AST/Codegen/CLI)
- UTF-8 in/out
- Overwrite existing files (no accidental appends)
- Helpful error messages (VS Code/Visual Studio friendly)

Language features
- Files define one or more functions:   func <name>([params]) { ... }
- Scoreboard setup:                      obj <objective>(<criterion>)[, ...];
  * criterion optional: "obj foo;" == "obj foo(dummy);"
- Declare scoreboard players (init 0):   var <objective:name>[, ...];
- Mutations:
  * <obj:name> += <int> ;   <obj:name> -= <int> ;
  * <obj:name> = <int> ;
  * <obj:dst> = <obj:src> ;
  * <obj:dst> = <obj:l> + <obj:r> ;  (also with '-')
- Const to marker NBT:                  const <name> = <number | "string"> ;
- If/While:
  * if(<obj:name> <op> <int|obj:rhs>) { ... }  where op ∈ {==, !=, <, <=, >, >=}
  * while(<obj:name>) { ... }  (loops while score >= 1)
- Random:                               rand(<obj:name>[, min, max]);
- Run passthrough:                       run("literal command");
- Interpolated tellraw:
  * run(v"say ...[objective:player] ..."); // auto-converts to tellraw
  * show(v"... [objective:player] ...");  // always tellraw
- Title:                                 title(title, "text");   (no newlines)
- Call:                                  call <func>(args...);    (runtime function call)

Quality-of-life
- Statement terminator: ';' OR newline (both accepted)
- Works with CRLF/Windows newlines
- Single-line comments: // ...

MIT License (c) 2025 프로젝트-MCFN
"""
from __future__ import annotations
import os, re, sys, json
from dataclasses import dataclass, field
from typing import List, Dict, Tuple, Optional, Union

# ================================================================
# Lexer
# ================================================================
# NOTE: Order matters! Longer tokens first (e.g., "+=", "-=") and ("<=", ">=", "==", "!=") before single-char.
TOKEN_SPEC: List[Tuple[str, str]] = [
    ("WS",       r"[ \t]+"),
    ("COMMENT",  r"//[^\n]*"),
    ("NEWLINE",  r"\r?\n"),
    # Keywords
    ("FUNC",     r"\bfunc\b"),
    ("OBJ",      r"\bobj\b"),
    ("VAR",      r"\bvar\b"),
    ("CONST",    r"\bconst\b"),
    ("IF",       r"\bif\b"),
    ("WHILE",    r"\bwhile\b"),
    ("RUN",      r"\brun\b"),
    ("CALL",     r"\bcall\b"),
    ("SHOW",     r"\bshow\b"),
    ("TITLE",    r"\btitle\b"),
    ("RAND",     r"\brand\b"),
    # Operators (multi-char first)
    ("LE",       r"<="),
    ("GE",       r">="),
    ("EQ",       r"=="),
    ("NE",       r"!="),
    ("LT",       r"<"),
    ("GT",       r">"),
    ("PLUSEQ",   r"\+="),
    ("MINUSEQ",  r"-="),
    ("PLUS",     r"\+"),
    ("MINUS",    r"-"),
    # Delims
    ("LPAREN",   r"\("),
    ("RPAREN",   r"\)"),
    ("LBRACE",   r"\{"),
    ("RBRACE",   r"\}"),
    ("COLON",    r":"),
    ("COMMA",    r","),
    ("SEMI",     r";"),
    ("ASSIGN",   r"="),
    # Strings
    ("VSTRING",  r'v"([^"\\]|\\.)*"'),
    ("STRING",   r'"([^"\\]|\\.)*"'),
    # Ids & numbers
    ("NUMBER",   r"\d+"),
    ("IDENT",    r"[A-Za-z_][A-Za-z0-9_]*"),
]
TOKEN_RE = re.compile("|".join(f"(?P<{n}>{p})" for n,p in TOKEN_SPEC))

@dataclass
class Tok:
    kind: str
    val: str
    pos: int
    line: int
    col: int

def lex(src: str) -> List[Tok]:
    toks: List[Tok] = []
    line, col, i = 1, 1, 0
    while i < len(src):
        m = TOKEN_RE.match(src, i)
        if not m:
            raise SyntaxError(f"Lex error at line {line}, col {col}: {src[i:i+20]!r}")
        kind = m.lastgroup
        val = m.group()
        if kind == "NEWLINE":
            line += 1
            col = 1
        elif kind not in ("WS", "COMMENT"):
            toks.append(Tok(kind, val, i, line, col))
        i = m.end()
        if kind != "NEWLINE":
            col += (i - m.start())
    toks.append(Tok("EOF", "", i, line, col))
    return toks

# ================================================================
# AST
# ================================================================
@dataclass
class ScoreRef:
    obj: str
    name: str

@dataclass
class Stmt:
    pass

@dataclass
class S_Obj(Stmt):
    pairs: List[Tuple[str, str]]  # (objective, criterion)

@dataclass
class S_Var(Stmt):
    inits: List[ScoreRef]

@dataclass
class S_Add(Stmt):  # += (positive or negative value); negative -> remove
    ref: ScoreRef
    amount: int

@dataclass
class S_Set(Stmt):  # = number
    ref: ScoreRef
    value: int

@dataclass
class S_SetCopy(Stmt):  # dst = src
    target: ScoreRef
    src: ScoreRef

@dataclass
class S_SetBinOp(Stmt):  # dst = left (+|-) right
    target: ScoreRef
    left: ScoreRef
    op: str  # '+' or '-'
    right: ScoreRef

@dataclass
class S_Const(Stmt):  # const name = number | "string"
    name: str
    value: Union[int, str]

@dataclass
class S_Run(Stmt):  # run("...") | run(v"...")
    text: str
    is_v: bool

@dataclass
class S_Show(Stmt):  # show(v"...")
    text: str

@dataclass
class S_Title(Stmt):  # title(title, "...")
    mode: str
    text: str

@dataclass
class S_If(Stmt):  # if(scoreRef op (number|scoreRef)) { body }
    ref: ScoreRef
    op: str  # '==','!=','<','<=','>','>='
    rhs_num: Optional[int] = None
    rhs_ref: Optional[ScoreRef] = None
    body: List[Stmt] = field(default_factory=list)

@dataclass
class S_While(Stmt):
    ref: ScoreRef  # while(scoreRef) { ... } — loop while score >= 1
    body: List[Stmt]

@dataclass
class S_Rand(Stmt):  # rand(scoreRef[, min, max])
    ref: ScoreRef
    min_val: Optional[int] = None
    max_val: Optional[int] = None

@dataclass
class S_Call(Stmt):
    target: str
    args: Dict[str, ScoreRef]  # currently unused in codegen (runtime call only)

@dataclass
class Func:
    name: str
    params: List[str]
    body: List[Stmt]

# ================================================================
# Parser
# ================================================================
class Parser:
    def __init__(self, toks: List[Tok]):
        self.toks = toks
        self.i = 0

    # ----- cursor helpers -----
    def cur(self) -> Tok:
        return self.toks[self.i]

    def eat(self, kind: str) -> Tok:
        t = self.cur()
        if t.kind != kind:
            raise SyntaxError(f"Expected {kind} at line {t.line}, col {t.col}, got {t.kind}")
        self.i += 1
        return t

    def match(self, kind: str) -> bool:
        if self.cur().kind == kind:
            self.i += 1
            return True
        return False

    # ----- QoL helpers -----
    def eat_stmt_end(self):
        # Prefer ';' if present
        if self.match("SEMI"):
            while self.cur().kind == "NEWLINE":
                self.i += 1
            return
        # Otherwise allow newline or block end/EOF
        if self.cur().kind in ("NEWLINE", "RBRACE", "EOF"):
            while self.cur().kind == "NEWLINE":
                self.i += 1
            return
        t = self.cur()
        raise SyntaxError(f"Expected ';' or newline at line {t.line}, col {t.col}, got {t.kind}")

    def parse_int(self) -> int:
        sign = -1 if self.match("MINUS") else 1
        n = int(self.eat("NUMBER").val)
        return sign * n

    def parse_score_ref(self) -> ScoreRef:
        obj = self.eat("IDENT").val
        self.eat("COLON")
        name = self.eat("IDENT").val
        return ScoreRef(obj, name)

    # ----- entry points -----
    def parse(self) -> List[Func]:
        funcs: List[Func] = []
        while self.cur().kind != "EOF":
            # Skip stray newlines between top-level functions
            while self.cur().kind == "NEWLINE":
                self.i += 1
            if self.cur().kind == "EOF":
                break
            funcs.append(self.parse_func())
        return funcs

    def parse_func(self) -> Func:
        self.eat("FUNC")
        name = self.eat("IDENT").val
        self.eat("LPAREN")
        params: List[str] = []
        if self.cur().kind == "IDENT":
            params.append(self.eat("IDENT").val)
            while self.match("COMMA"):
                params.append(self.eat("IDENT").val)
        self.eat("RPAREN")
        self.eat("LBRACE")
        body = self.parse_stmts()
        self.eat("RBRACE")
        return Func(name, params, body)

    def parse_stmts(self) -> List[Stmt]:
        out: List[Stmt] = []
        # Leading blank lines
        while self.cur().kind == "NEWLINE":
            self.i += 1
        while self.cur().kind not in ("RBRACE", "EOF"):
            # Skip blank lines between statements
            while self.cur().kind == "NEWLINE":
                self.i += 1
            k = self.cur().kind
            if   k == "OBJ":   out.append(self.parse_obj())
            elif k == "VAR":   out.append(self.parse_var())
            elif k == "CONST": out.append(self.parse_const())
            elif k == "IF":    out.append(self.parse_if())
            elif k == "WHILE": out.append(self.parse_while())
            elif k == "RUN":   out.append(self.parse_run())
            elif k == "SHOW":  out.append(self.parse_show())
            elif k == "TITLE": out.append(self.parse_title())
            elif k == "RAND":  out.append(self.parse_rand())
            elif k == "CALL":  out.append(self.parse_call())
            elif k == "IDENT": out.append(self.parse_score_stmt())
            else:
                t = self.cur()
                raise SyntaxError(f"Unexpected token {t.kind} at line {t.line}")
            # Trailing blank lines after a statement
            while self.cur().kind == "NEWLINE":
                self.i += 1
        return out

    # ----- statements -----
    def parse_obj(self) -> S_Obj:
        self.eat("OBJ")
        pairs: List[Tuple[str, str]] = []
        while True:
            obj = self.eat("IDENT").val
            if self.match("LPAREN"):
                crit = self.eat("IDENT").val
                self.eat("RPAREN")
            else:
                crit = "dummy"  # default criterion
            pairs.append((obj, crit))
            if not self.match("COMMA"):
                break
        self.eat_stmt_end()
        return S_Obj(pairs)

    def parse_var(self) -> S_Var:
        self.eat("VAR")
        inits: List[ScoreRef] = []
        while True:
            inits.append(self.parse_score_ref())
            if not self.match("COMMA"):
                break
        self.eat_stmt_end()
        return S_Var(inits)

    def parse_const(self) -> S_Const:
        self.eat("CONST")
        name = self.eat("IDENT").val
        self.eat("ASSIGN")
        t = self.cur()
        if t.kind == "STRING":
            # use common string parser (but forbid v-strings)
            text, is_v = self._parse_string_like()
            if is_v:
                raise SyntaxError("const는 v\"...\"를 지원하지 않습니다.")
            self.eat_stmt_end()
            return S_Const(name, text)
        elif t.kind in ("NUMBER", "MINUS"):
            num = self.parse_int()
            self.eat_stmt_end()
            return S_Const(name, num)
        else:
            raise SyntaxError("const 값은 숫자 또는 \"문자열\"이어야 합니다.")

    def parse_score_stmt(self) -> Stmt:
        # LHS
        lhs = self.parse_score_ref()
        # += / -=
        if self.match("PLUSEQ"):
            num = self.parse_int()
            self.eat_stmt_end()
            return S_Add(lhs, +num)
        if self.match("MINUSEQ"):
            num = self.parse_int()
            self.eat_stmt_end()
            return S_Add(lhs, -num)
        # =
        self.eat("ASSIGN")
        # number
        if self.cur().kind in ("NUMBER", "MINUS"):
            num = self.parse_int()
            self.eat_stmt_end()
            return S_Set(lhs, num)
        # scoreRef (+|- scoreRef)?
        r1 = self.parse_score_ref()
        if self.match("PLUS"):
            r2 = self.parse_score_ref()
            self.eat_stmt_end()
            return S_SetBinOp(lhs, r1, "+", r2)
        if self.match("MINUS"):
            r2 = self.parse_score_ref()
            self.eat_stmt_end()
            return S_SetBinOp(lhs, r1, "-", r2)
        self.eat_stmt_end()
        return S_SetCopy(lhs, r1)

    def parse_if(self) -> S_If:
        self.eat("IF")
        self.eat("LPAREN")
        ref, op, rhs_num, rhs_ref = self.parse_comp()
        self.eat("RPAREN")
        self.eat("LBRACE")
        body = self.parse_stmts()
        self.eat("RBRACE")
        return S_If(ref, op, rhs_num, rhs_ref, body)

    def parse_while(self) -> S_While:
        self.eat("WHILE")
        self.eat("LPAREN")
        ref = self.parse_score_ref()
        self.eat("RPAREN")
        self.eat("LBRACE")
        body = self.parse_stmts()
        self.eat("RBRACE")
        return S_While(ref, body)

    def _parse_string_like(self) -> Tuple[str, bool]:
        t = self.cur()
        if t.kind == "VSTRING":
            self.i += 1
            return t.val[2:-1], True  # strip v"..."
        if t.kind == "STRING":
            self.i += 1
            return t.val[1:-1], False
        raise SyntaxError(f"Expected string at line {t.line}")

    def parse_run(self) -> S_Run:
        self.eat("RUN")
        self.eat("LPAREN")
        text, is_v = self._parse_string_like()
        self.eat("RPAREN")
        self.eat_stmt_end()
        return S_Run(text, is_v)

    def parse_show(self) -> S_Show:
        self.eat("SHOW")
        self.eat("LPAREN")
        text, is_v = self._parse_string_like()
        if not is_v:
            raise SyntaxError("show(...) must use v\"...\" for interpolation")
        self.eat("RPAREN")
        self.eat_stmt_end()
        return S_Show(text)

    def parse_title(self) -> S_Title:
        self.eat("TITLE")
        self.eat("LPAREN")
        mode = self.eat("IDENT").val
        self.eat("COMMA")
        text, is_v = self._parse_string_like()
        if is_v:
            raise SyntaxError("title(...) does not support v-strings")
        self.eat("RPAREN")
        self.eat_stmt_end()
        return S_Title(mode, text)

    def parse_call(self) -> S_Call:
        self.eat("CALL")
        target = self.eat("IDENT").val
        self.eat("LPAREN")
        args: Dict[str, ScoreRef] = {}
        if self.cur().kind != "RPAREN":
            while True:
                p = self.eat("IDENT").val
                self.eat("ASSIGN")
                args[p] = self.parse_score_ref()
                if not self.match("COMMA"):
                    break
        self.eat("RPAREN")
        # Optional trailing comma before ';'
        self.match("COMMA")
        self.eat_stmt_end()
        return S_Call(target, args)

    def parse_rand(self) -> S_Rand:
        self.eat("RAND")
        self.eat("LPAREN")
        ref = self.parse_score_ref()
        min_v: Optional[int] = None
        max_v: Optional[int] = None
        if self.match("COMMA"):
            min_v = self.parse_int()
            self.eat("COMMA")
            max_v = self.parse_int()
        self.eat("RPAREN")
        self.eat_stmt_end()
        return S_Rand(ref, min_v, max_v)

    def parse_comp(self) -> Tuple[ScoreRef, str, Optional[int], Optional[ScoreRef]]:
        lhs = self.parse_score_ref()
        if   self.match("EQ"): op = "=="
        elif self.match("LE"): op = "<="
        elif self.match("GE"): op = ">="
        elif self.match("LT"): op = "<"
        elif self.match("GT"): op = ">"
        elif self.match("NE"): op = "!="
        else: raise SyntaxError("Expected one of ==, <=, >=, <, >, !=")
        # RHS: number or scoreRef
        if self.cur().kind in ("NUMBER", "MINUS"):
            num = self.parse_int()
            return lhs, op, num, None
        rhs = self.parse_score_ref()
        return lhs, op, None, rhs

# ================================================================
# Code generator
# ================================================================
INT_MIN, INT_MAX = -2147483648, 2147483647

@dataclass
class Ctx:
    namespace: str
    outdir: str
    if_counter: int = 0
    while_counter: int = 0
    funcs: Dict[str, Func] = field(default_factory=dict)
    param_bind: Dict[str, ScoreRef] = field(default_factory=dict)  # reserved for future param interpolation
    current_func: str = ""

def ensure_dir(p: str):
    os.makedirs(os.path.dirname(p), exist_ok=True)

def clear_file(path: str):
    ensure_dir(path)
    open(path, "w", encoding="utf-8").close()

def emit_line(path: str, s: str):
    ensure_dir(path)
    with open(path, "a", encoding="utf-8") as f:
        f.write(s.rstrip() + "\n")

def mc_path(ctx: Ctx, *parts: str) -> str:
    return os.path.join(ctx.outdir, ctx.namespace, *parts)

def matches_expr(op: str, num: int) -> Optional[str]:
    # Convert numeric comparison to a matches range string; return None if impossible (always false)
    if op == "==":
        return f"{num}..{num}"
    if op == "<=":
        return f"..{num}"
    if op == ">=":
        return f"{num}.."
    if op == "<":
        if num <= INT_MIN:
            return None
        return f"..{num-1}"
    if op == ">":
        if num >= INT_MAX:
            return None
        return f"{num+1}.."
    raise ValueError(f"matches_expr: unsupported op {op}")

def interpolate_json(text: str, binding: Dict[str, ScoreRef]) -> List[dict]:
    """
    v-string interpolation:
      - [ParamName]           -> if in binding, use its ScoreRef
      - [objective:player]    -> explicit scoreboard component
      - otherwise             -> keep literal [ ... ] as text
    """
    parts: List[dict] = []
    i = 0
    pattern = re.compile(r"\[([A-Za-z_][A-Za-z0-9_]*)(?::([A-Za-z_][A-Za-z0-9_]*))?\]")
    for m in pattern.finditer(text):
        if m.start() > i:
            parts.append({"text": text[i:m.start()]})
        key1 = m.group(1)
        key2 = m.group(2)
        if key2 is not None:
            parts.append({"score": {"name": key2, "objective": key1}})
        elif key1 in binding:
            ref = binding[key1]
            parts.append({"score": {"name": ref.name, "objective": ref.obj}})
        else:
            parts.append({"text": m.group(0)})
        i = m.end()
    if i < len(text):
        parts.append({"text": text[i:]})
    return parts or [{"text": ""}]

def emit_block(ctx: Ctx, path: str, body: List[Stmt]):
    for s in body:
        if isinstance(s, S_Obj):
            for obj, crit in s.pairs:
                emit_line(path, f"scoreboard objectives add {obj} {crit}")
        elif isinstance(s, S_Var):
            for r in s.inits:
                emit_line(path, f"scoreboard players set {r.name} {r.obj} 0")
        elif isinstance(s, S_Add):
            if s.amount >= 0:
                emit_line(path, f"scoreboard players add {s.ref.name} {s.ref.obj} {s.amount}")
            else:
                emit_line(path, f"scoreboard players remove {s.ref.name} {s.ref.obj} {abs(s.amount)}")
        elif isinstance(s, S_Set):
            emit_line(path, f"scoreboard players set {s.ref.name} {s.ref.obj} {s.value}")
        elif isinstance(s, S_SetCopy):
            emit_line(path, f"scoreboard players operation {s.target.name} {s.target.obj} = {s.src.name} {s.src.obj}")
        elif isinstance(s, S_SetBinOp):
            emit_line(path, f"scoreboard players operation {s.target.name} {s.target.obj} = {s.left.name} {s.left.obj}")
            op = "+=" if s.op == "+" else "-="
            emit_line(path, f"scoreboard players operation {s.target.name} {s.target.obj} {op} {s.right.name} {s.right.obj}")
        elif isinstance(s, S_Const):
            tag = f"MCFN_{ctx.current_func}"
            emit_line(path, f"execute unless entity @e[type=minecraft:marker,tag={tag},limit=1] run summon minecraft:marker ~ ~ ~ {{Tags:[\"{tag}\"]}}")
            if isinstance(s.value, int):
                vtxt = str(s.value)
            else:
                vtxt = json.dumps(s.value, ensure_ascii=False)
            emit_line(path, f"data merge entity @e[type=minecraft:marker,tag={tag},limit=1] {{MCFN:{{{s.name}:{vtxt}}}}}")
        elif isinstance(s, S_Run):
            if s.is_v:
                # Special-case: if starts with 'say ', render as tellraw so we can embed score components
                st = s.text.strip()
                if st.startswith("say "):
                    msg = st[4:]
                    comps = interpolate_json(msg, ctx.param_bind)
                    emit_line(path, f"tellraw @a {json.dumps(comps, ensure_ascii=False)}")
                else:
                    comps = interpolate_json(s.text, ctx.param_bind)
                    emit_line(path, f"tellraw @a {json.dumps(comps, ensure_ascii=False)}")
            else:
                emit_line(path, s.text)
        elif isinstance(s, S_Show):
            comps = interpolate_json(s.text, ctx.param_bind)
            emit_line(path, f"tellraw @a {json.dumps(comps, ensure_ascii=False)}")
        elif isinstance(s, S_Title):
            if "\n" in s.text:
                raise ValueError("title(...) 문자열에 개행(\\n)은 허용되지 않습니다.")
            emit_line(path, f"title @a title {json.dumps({'text': s.text}, ensure_ascii=False)}")
        elif isinstance(s, S_If):
            ctx.if_counter += 1
            fname = f"ifs/if_{ctx.if_counter}.mcfunction"
            subpath = mc_path(ctx, fname)
            clear_file(subpath)
            emit_block(ctx, subpath, s.body)
            if s.rhs_ref is not None:
                if s.op == "!=":
                    emit_line(path, f"execute unless score {s.ref.name} {s.ref.obj} = {s.rhs_ref.name} {s.rhs_ref.obj} run function {ctx.namespace}:{fname[:-11]}")
                else:
                    mc_op = {"==": "=", "<": "<", "<=": "<=", ">": ">", ">=": ">="}[s.op]
                    emit_line(path, f"execute if score {s.ref.name} {s.ref.obj} {mc_op} {s.rhs_ref.name} {s.rhs_ref.obj} run function {ctx.namespace}:{fname[:-11]}")
            else:
                if s.op == "!=":
                    rng = f"{s.rhs_num}..{s.rhs_num}"
                    emit_line(path, f"execute unless score {s.ref.name} {s.ref.obj} matches {rng} run function {ctx.namespace}:{fname[:-11]}")
                else:
                    rng = matches_expr(s.op, s.rhs_num)
                    if rng is not None:
                        emit_line(path, f"execute if score {s.ref.name} {s.ref.obj} matches {rng} run function {ctx.namespace}:{fname[:-11]}")
                    # else impossible condition -> no emission
        elif isinstance(s, S_While):
            ctx.while_counter += 1
            fname = f"whiles/while_{ctx.while_counter}.mcfunction"
            subpath = mc_path(ctx, fname)
            clear_file(subpath)
            emit_block(ctx, subpath, s.body)
            emit_line(subpath, f"execute if score {s.ref.name} {s.ref.obj} matches 1.. run function {ctx.namespace}:{fname[:-11]}")
            emit_line(path, f"function {ctx.namespace}:{fname[:-11]}")
        elif isinstance(s, S_Rand):
            lo = 0 if s.min_val is None else s.min_val
            hi = 100 if s.max_val is None else s.max_val
            emit_line(path, f"scoreboard players random {s.ref.name} {s.ref.obj} {lo} {hi}")
        elif isinstance(s, S_Call):
            # Runtime-only function call; parameter interpolation across functions is not yet supported.
            emit_line(path, f"function {ctx.namespace}:{s.target}")
        else:
            raise RuntimeError("Unhandled statement type in codegen")

# ================================================================
# Compile
# ================================================================

def compile_funcs(funcs: List[Func], namespace: str = "namespace", outdir: str = "out"):
    ctx = Ctx(namespace, outdir)
    # index
    for f in funcs:
        ctx.funcs[f.name] = f
    # emit each function
    for f in funcs:
        ctx.current_func = f.name
        path = mc_path(ctx, f"{f.name}.mcfunction")
        clear_file(path)
        ctx.param_bind = {}  # reserved for future call-arg interpolation
        emit_block(ctx, path, f.body)


def transpile(src: str, namespace: str = "namespace", outdir: str = "out"):
    toks = lex(src)
    funcs = Parser(toks).parse()
    compile_funcs(funcs, namespace, outdir)

# ================================================================
# CLI
# ================================================================
HELP = """Usage:
  python mcfndsl.py <input_file> [--ns <namespace>] [--out <outdir>]

Notes:
- File extension is accepted: .mcfn
- Outputs to <outdir>/<namespace>/*.mcfunction and subfolders.
"""

def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print(HELP)
        return
    inp = sys.argv[1]
    ns = "namespace"
    out = "out"
    if "--ns" in sys.argv:
        ns = sys.argv[sys.argv.index("--ns") + 1]
    if "--out" in sys.argv:
        out = sys.argv[sys.argv.index("--out") + 1]
    
    # 확장자 검사
    if not inp.endswith(".mcfn"):
        print("Error: 확장자는 .mcfn 이어야 합니다.")
        return

    try:
        with open(inp, "r", encoding="utf-8") as f:
            src = f.read()
        transpile(src, ns, out)
        print(f"OK: wrote .mcfunction files under {out}/{ns}/")
    except Exception as e:
        # VS Code/Visual Studio friendly error format
        msg = str(e)
        m = re.search(r"line (\d+), col (\d+)", msg)
        if m:
            line, col = m.group(1), m.group(2)
            print(f"{inp}:{line}:{col}: error: {msg}")
        else:
            m2 = re.search(r"Lex error at line (\d+), col (\d+)", msg)
            if m2:
                line, col = m2.group(1), m2.group(2)
                print(f"{inp}:{line}:{col}: error: {msg}")
            else:
                print(f"{inp}:0:0: error: {msg}")
        sys.exit(1)

if __name__ == "__main__":
    main()
