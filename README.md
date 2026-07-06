# 찬양 콘티 자동 추천기 + 유튜브 기반 DB 수집기

이 프로그램은 **YouTube Data API로 찬양 영상 후보를 자동 수집**하고, **NVIDIA API로 곡명/느림·빠름/분위기/여러 곡 포함 여부를 보조 분석**한 뒤, 사람이 실제 예배에 필요한 값만 검수해서 콘티를 추천하는 Streamlit 웹앱입니다.

## 핵심 구조

1. **유튜브 기반 자동 DB 입력**
   - 어노이팅, 마커스워십, 피아워십, 나비워십, 브리지임팩트, 청춘찬양단, 아가파오 워십 등 기본 찬양팀 목록 제공
   - 찬양팀 우선순위 없이, 선택한 모든 찬양팀/검색어를 동일하게 수집
   - 사용자가 직접 찬양팀과 검색어를 추가/수정 가능
   - 검색어당 수집 개수 제한 없이 YouTube `nextPageToken`을 따라 가능한 페이지를 끝까지 수집
   - YouTube 검색으로 제목, 채널, 링크, 썸네일, 업로드일, 영상 길이 저장
   - 예배실황/메들리처럼 영상 하나에 여러 곡이 들어 있으면 같은 유튜브 영상에서 곡별 행으로 분리 저장 가능

2. **NVIDIA AI 보조**
   - 기본 Base URL: `https://integrate.api.nvidia.com/v1`
   - 기본 모델: `minimaxai/minimax-m3`
   - 자동 수집 시 곡명, 빠르기, 주제 추정
   - 설명란 타임스탬프 또는 제목의 `곡A+곡B+곡C` 형태를 참고해서 여러 곡 분리 저장
   - 콘티 추천 후 구간별 흐름 설명 생성 가능

3. **검수형 DB**
   - 빠르기, 키, BPM, 첫 코드, 마지막 코드, 주제는 사람이 확인 후 저장
   - 악보/코드 악보는 필수 아님
   - 검수 완료된 곡만 콘티 추천에 사용

4. **콘티 추천**
   - 예: 앞부분 느린곡 3곡 이어서 / 중간 빠른곡 3곡 이어서 / 마지막 느린곡 1곡 끊어서
   - 구간별 원하는 분위기와 주제를 따로 설정 가능
   - 참고할 찬양팀 선택 가능
   - 이어부르기 구간은 키, BPM, 첫 코드, 마지막 코드, 주제 유사도를 점수화
   - 결과는 순서, 곡명, 찬양팀, 키, BPM, 주제, 유튜브 링크 중심으로 출력

## 설치 방법

### 1. Python 설치
Python 3.10 이상을 설치하세요.

### 2. 폴더 열기
압축을 푼 폴더에서 명령 프롬프트 또는 PowerShell을 엽니다.

### 3. 필요한 패키지 설치

```bash
pip install -r requirements.txt
```

## API 키 설정: secrets.toml 방식

이 앱은 Streamlit 웹앱으로 쓰는 것을 기준으로 만들었습니다. API 키는 코드에 직접 넣지 말고 `secrets.toml` 또는 Streamlit Cloud Secrets에 넣으세요.

### 로컬 실행

1. 프로젝트 폴더 안에 `.streamlit` 폴더를 만듭니다.
2. `.streamlit/secrets.toml.example` 파일을 참고해서 `.streamlit/secrets.toml` 파일을 만듭니다.
3. 아래처럼 입력합니다.

```toml
YOUTUBE_API_KEY = "YOUR_YOUTUBE_DATA_API_KEY"
NVIDIA_API_KEY = "YOUR_NVIDIA_API_KEY"
NVIDIA_BASE_URL = "https://integrate.api.nvidia.com/v1"
NVIDIA_MODEL = "minimaxai/minimax-m3"
```

또는 섹션 형태도 가능합니다.

```toml
[youtube]
api_key = "YOUR_YOUTUBE_DATA_API_KEY"

[nvidia]
api_key = "YOUR_NVIDIA_API_KEY"
base_url = "https://integrate.api.nvidia.com/v1"
model = "minimaxai/minimax-m3"
```

중요: `.streamlit/secrets.toml` 파일은 절대 깃허브에 올리지 마세요. `.gitignore`에 이미 제외해두었습니다.

### Streamlit Cloud 배포

1. GitHub 저장소에 이 프로젝트를 올립니다.
2. Streamlit Cloud에서 새 앱을 생성합니다.
3. Main file path를 `app.py`로 지정합니다.
4. App settings > Secrets에 로컬 `secrets.toml` 내용을 그대로 붙여넣습니다.
5. Deploy 합니다.

## 실행

```bash
streamlit run app.py
```

## 사용 순서

1. 왼쪽 메뉴에서 **1. DB 자동 수집** 선택
2. 기본값은 모든 찬양팀 선택입니다. 필요한 경우만 선택을 줄이거나 **자동 수집 찬양팀 직접 추가/수정**에서 새 팀 추가
3. 수집은 검색어당 50개씩 페이지를 넘기며 끝까지 진행됩니다. quota/오류로 멈추면 다음 실행 때 이어서 수집할 수 있습니다.
4. `NVIDIA AI로 DB 자동 분류` 체크
5. `예배실황/메들리 영상은 여러 곡으로 분리 저장` 체크
6. 수집 시작
7. 영상 하나를 직접 넣고 싶으면 **유튜브 URL 직접 분석/저장**에 링크 붙여넣기
8. **2. 곡 검수**에서 키/BPM/첫 코드/마지막 코드/주제 확인
9. 검수 완료 저장
10. **3. 콘티 추천**에서 구간별 빠르기, 곡 수, 이어부르기 여부, 분위기/주제 선택
11. 추천 결과에서 순서와 유튜브 링크 확인


## 전체 수집 방식

이 버전은 “검색어당 10개/20개만 수집” 같은 제한을 두지 않습니다.

- YouTube 검색 1페이지는 50개로 요청합니다.
- 응답에 `nextPageToken`이 있으면 다음 페이지를 계속 요청합니다.
- 더 이상 `nextPageToken`이 없으면 해당 검색어는 완료 처리됩니다.
- Google/YouTube quota 초과, 네트워크 오류, API 오류가 나면 현재 페이지 토큰을 저장하고 멈춥니다.
- 다음에 다시 실행하면 **이전 중단 지점부터 이어서 수집**할 수 있습니다.
- 중복 영상/곡은 DB의 `video_id` 기준으로 건너뜁니다.
- 찬양팀 우선순위는 사용하지 않습니다. `worship_sources.csv`에는 `team`, `search_queries`, `notes`만 사용합니다.

기본은 모든 영상을 저장하는 방식입니다. 커버/MR/너무 짧거나 긴 영상을 제외하고 싶을 때만 **제외 키워드/길이 필터 적용**을 켜세요.

## 수집 진행상태 확인

왼쪽 메뉴 **4. DB 관리**에서 검색어별 진행상태를 볼 수 있습니다.

- `completed = 1`: 해당 검색어는 끝까지 수집 완료
- `completed = 0`: quota 초과/오류/중단으로 이어서 수집 가능
- `next_page_token`: 다음 실행 때 이어갈 위치
- `pages_fetched`: 지금까지 가져온 검색 페이지 수
- `videos_seen`: 지금까지 확인한 영상 수
- `songs_inserted`: 신규 저장된 곡 수
- `songs_skipped`: 중복/제외된 곡 수

## 여러 곡이 한 영상에 들어있는 경우

예를 들어 유튜브 제목이 아래처럼 되어 있으면:

```text
생명 주께 있네+다와서 찬양해+주 우리 아버지+왕의 왕 주의 주+그리 아니하실지라도 - 피아워십
```

앱은 같은 원본 영상 ID를 `source_video_id`로 보관하고, DB에는 아래처럼 곡별로 저장합니다.

```text
source_video_id = buQeXNEejWg
video_id = buQeXNEejWg__song_01 / clean_title = 생명 주께 있네
video_id = buQeXNEejWg__song_02 / clean_title = 다와서 찬양해
video_id = buQeXNEejWg__song_03 / clean_title = 주 우리 아버지
...
```

설명란에 타임스탬프가 있으면 유튜브 링크도 `&t=123s` 형태로 저장합니다. 타임스탬프가 없으면 같은 영상 링크로 저장하고, 곡 순서만 구분합니다.

## 예시 콘티 조건

- 구간 1: 느린곡 3곡 / 이어부르기 / 은혜, 임재, 회복, 경배
- 구간 2: 빠른곡 3곡 / 이어부르기 / 찬양, 기쁨, 감사, 선포, 승리
- 구간 3: 느린곡 1곡 / 끊어서 / 결단, 기도, 임재, 회복

## 주의사항

- 이 프로그램은 악보나 가사를 무단으로 크롤링하거나 저장하지 않습니다.
- YouTube 검색 결과와 AI 추정 결과는 완벽하지 않으므로 반드시 검수 단계를 거쳐야 합니다.
- “제한 없이 전체 수집”으로 설정했더라도 YouTube Data API의 일일 quota와 YouTube가 검색 API로 제공하는 페이지 범위는 넘길 수 없습니다. quota가 끝나면 다음 실행 때 이어서 수집하세요.
- NVIDIA API는 곡명/분위기/여러 곡 분리 보조용입니다. 키, BPM, 첫 코드, 마지막 코드는 실제 예배팀 기준으로 검수하는 것을 권장합니다.
