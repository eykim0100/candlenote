# 캔들노트 — Claude 작업 안내

큰 움직임이 있었던 날(갭 ±3%, 등락 ±5%, 거래량 20일 평균 3배)의 뉴스·공시를 캔들 위에서 보여주는 주식 차트 사이트.
진행 상황과 결정 사항은 `진행상황.md`를 먼저 읽을 것.

## 구조
- `index.html` — 화면 전체 (한 파일, lightweight-charts 4.1.3 CDN). `data/kr/index.json`이 없으면 샘플 데이터로 표시
- `scripts/collect_kr.py` — 국내 데이터 수집 (공공데이터 시세 → 시총 상위 100, DART 재무·배당·기업정보·공시, NAVER API HUB 뉴스)
- `.github/workflows/update.yml` — 평일 14:07 KST 자동 실행, `data/` 커밋 (`git pull --rebase` 후 push)
- `data/kr/*.json` — 수집 결과 (자동 생성, 직접 고치지 말 것)
- `data/kr_notes.json`, `data/kr_pins.json`, `data/sector_avg.json` — 사이트 편집 모드에서 사용자가 직접 입력한 값 (GitHub API로 저장됨)

## 규칙
- GitHub 저장소(eykim0100/candlenote, main)가 원본. 작업 전 `git pull`
- API 키는 GitHub Secrets에만: DATA_GO_KR_KEY(Decoding 키), DART_API_KEY, NAVER_CLIENT_ID, NAVER_CLIENT_SECRET, FINNHUB_API_KEY. 로컬 테스트용 키는 `.env`에 두고 절대 커밋하지 말 것
- 뉴스 API는 무료 한도(하루 25,000회) 안에서만: 최근 이벤트 400회 + 과거 채우기 5,000회 상한 유지
- 기사 본문 복사 금지(제목·링크만). 화면 하단 데이터 출처 표시 유지. "투자 권유 아님" 문구 유지
- 사용자는 비개발자. 설명은 쉬운 한국어로, 바꾼 내용은 짧게 요약
- 바뀐 결정·남은 일은 `진행상황.md`에 반영
