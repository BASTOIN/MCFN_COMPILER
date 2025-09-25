# MCFN-DSL

MCFN-DSL은 간결한 문법으로 마인크래프트 .mcfunction 파일을 생성(트랜스파일)하는 도구입니다.
큐(queue)와 강한 대기(wait) 메커니즘을 내장해 복잡한 함수 체이닝과 비동기 흐름을 안전하게 작성할 수 있게 도와줍니다.


# SNIPPET

src 에 스니펫이 포함되어있습니다. 

`src/snippet/mcfn-vscode-ext/out/` 에 있는 자바스크립트를 비쥬얼 스튜디오 코드 내에서 실행시키세요.

필요 설치:
```npm
npm install
npm install vscode
```





## 주요 기능

사람 친화적인 DSL → .mcfunction 자동 생성

`call foo()[Q]` 형태의 강한 대기(wait) 지원: 호출된 함수 내부에서 열린 모든 하위 큐가 소진될 때까지 기다림

`if(...)[Q]{}` / `while(...)[Q]{}` 에도 wait 적용 가능

`runs{ ... }` 블록: 여러 줄 원시 명령 패스스루(마크 함수처럼 사용, v-string 미지원)

`run(v"...")` 보간: [objective:name] → tellraw score 컴포넌트로 치환

`rand(...)`, const, var, obj 등 기본 DSL 구성

## 사용 예시 (요약)
CLI 사용법
`python mcfndsl.py <input.mcfn> [--ns <namespace>] [--out <outdir>]`


`<input.mcfn>`: 변환할 DSL 파일 (확장자 .mcfn 필수)

`--ns <namespace>`: Minecraft 네임스페이스(기본: namespace)

`--out <outdir>`: 출력 디렉터리 이름(기본: out)

출력 경로는 <namespace>/<outdir>/... 형태로 생성됩니다.

## DSL 문법(핵심)
### 선언 / 초기화

`obj score(dummy), timer(air);`

`var score:player, timer:loop;`

`const GREETING = "Hello";`

`const MAX = 100;`

### 스코어 연산 / 대입
`score:player = 10;`

`score:player += 5;`

`score:player -= 2;`

`score:out = score:a + score:b;`

`score:out = score:a - score:b;`

`score:dst = score:src;`   # 복사

### 난수
`rand(score:rand);`           # 0..100

`rand(score:rand, 5, 12);`    # 5..12

## 조건 / 반복

비교 연산자: ==, !=, <, <=, >, >=
```mcfn
if(score:player == 3) {
    run("say eq 3");
}

while(score:flag) {
    run(v"say [score:flag]");
}
```
## 큐(세그먼트) 표기

세그먼트 전환을 위해 if/while/call 뒤에 [Qname] 표기 가능

`if(a:x != 0)[Q1] { ... }`   # Q1 슬롯에 등록, 다음 세그먼트로 분리
`call do_work()[Q2];`       # 강한 대기(wait)로 호출


`call foo()[Q]` 동작: foo() 내부에서 새로 열린 큐들이 전부 끝날 때까지 틱 단위로 대기 → 그 후 Q를 오픈해 다음 세그먼트로 진행

## 호출 / 함수
func main(){
  call sub();
}

func sub(){
  run("say sub!");
}

## 출력 / 보간
```mcfn
run("say hello");     # 그대로 실행
run(v"tellraw @a {text:\"[score:player]\"}");  # 보간 → score 컴포넌트 적용
show(v"현재값: [score:player]");               # tellraw shortcut
title(title, "게임 시작");                     # title 사용 (v-string 불가)
```

여러 줄 원시 명령: `runs{ ... }`

runs{} 안의 내용은 그대로 .mcfunction에 출력됩니다 (v-string 미지원).

내부 주석(// ...)과 빈 줄은 무시됩니다.

```mcfn
runs{
  say line1
  data get entity @s
  scoreboard players add p score 1
}
```
# 큐 & 대기(wait) 동작(요약)

내부 스코어보드: mcfq 사용

디스패처: queue/queue_main.mcfunction 자동 생성. 각 슬롯(예: Q1)이 0이면 해당 큐 파일을 실행.

`call foo()[Q]:`

caller에서 `function <ns>:foo` 실행

caller에 wait_* 함수가 스케줄되어 틱 단위로 queue_main 및 any_open을 확인

열린 슬롯(=0)이 하나도 없을 때만 Q를 0으로 내려 다음 세그먼트를 실행

`if(...)[Q]` / `while(...)[Q]` 역시 동일한 wait 방식으로 동작(조건이 참인 경우, 해당 블록은 즉시 실행되고 다음 세그먼트는 wait가 관리)

# 트랜스파일러 제약 & 주의점

입력 파일은 UTF-8 권장. 한글 사용 시 깨짐 주의.

DSL 파싱 오류는 CLI에서 줄/열 정보와 함께 표시됩니다.

Minecraft 내부 명령의 실행 오류(selector 오류 등)는 게임에서 해당 줄만 실패하고 이후 줄은 계속 실행됩니다.

무한 루프/과도한 스케줄 사용은 성능 문제를 유발하므로 방어책(락, 카운터)을 권장합니다.

예제: 전체 플로우
```mcfn
func main(){
  obj a(dummy); var a:x;
  a:x = 2;

  if(a:x == 2)[Q1]{
    runs{
      say entered if
      tellraw @a {"text":"score: [a:x]"}
    }
  }

  call worker()[Q2];

  run(v"tellraw @a {text:\"final: [a:x]\"}");
}

func worker(){
  runs{
    say working...
    scoreboard players add p a 1
  }
}
```

결과: if 블록과 worker() 내부 명령이 .mcfunction으로 분리 생성. Q1, Q2에 맞춰 큐 디스패처가 동작.

문제 해결 팁

`runs{}` 내부의 @a, @s 같은 selector가 파싱 문제를 일으키면 렉서에 @ 토큰이 포함되어 있는지 확인하세요.

생성된 .mcfunction 파일을 직접 `/function <ns>:...`로 실행해 동작 확인(개별 파일 단위 테스트).

큐 관련 문제: queue/queue_main.mcfunction, queue/any_open.mcfunction, queue/wait_* 파일들이 올바르게 생성됐는지 확인.

기여 & 개발

버그 리포트 및 기능 제안은 이 레포의 이슈 트래커를 이용하세요.

변경 흐름(권장):

코드 수정 → 내부 테스트 DSL로 변환 확인

생성된 .mcfunction을 마인크래프트 월드에서 테스트

린트/유닛 테스트 추가 환영.

라이선스
LICENCE.md 참고
