name: Lotte Cancel - Gyeongju 2026-09-10

on:
  workflow_dispatch:

  schedule:
    # KST 02:07 / 06:07 / 10:07 / 14:07 / 18:07 / 22:07
    # UTC 17:07 / 21:07 / 01:07 / 05:07 / 09:07 / 13:07
    - cron: "7 1,5,9,13,17,21 * * *"

permissions:
  contents: write

concurrency:
  group: lotte-cancel-gyeongju-20260910
  cancel-in-progress: false

jobs:
  monitor:
    runs-on: ubuntu-latest
    timeout-minutes: 245

    steps:
      - name: Checkout
        uses: actions/checkout@v4
        with:
          fetch-depth: 0

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Install dependencies
        run: |
          pip install -r requirements.txt

      - name: Set monitoring duration
        id: duration
        shell: bash
        run: |
          python - <<'PY'
          import os
          from datetime import datetime, timedelta
          from zoneinfo import ZoneInfo

          kst = ZoneInfo("Asia/Seoul")
          now = datetime.now(kst)

          # 다음 자동 시작 시각:
          # 02:07 / 06:07 / 10:07 / 14:07 / 18:07 / 22:07 KST
          hours = [2, 6, 10, 14, 18, 22]

          candidates = []
          for day_add in (0, 1):
              day = now.date() + timedelta(days=day_add)
              for hour in hours:
                  candidates.append(
                      datetime(
                          day.year, day.month, day.day,
                          hour, 7, 0,
                          tzinfo=kst,
                      )
                  )

          next_start = min(t for t in candidates if t > now)

          # 다음 작업 시작 90초 전까지 현재 작업이 감시.
          seconds = max(
              60,
              int((next_start - now).total_seconds()) - 90
          )

          print("KST NOW:", now.strftime("%Y-%m-%d %H:%M:%S"))
          print("NEXT AUTO:", next_start.strftime("%Y-%m-%d %H:%M:%S"))
          print("RUN_SECONDS:", seconds)

          with open(os.environ["GITHUB_OUTPUT"], "a") as f:
              f.write(f"seconds={seconds}\n")
          PY

      - name: Run monitor
        env:
          RUN_SECONDS: ${{ steps.duration.outputs.seconds }}
          CHECK_INTERVAL: "10"
          DISCORD_LOTTE_WORLDTOWER: ${{ secrets.DISCORD_LOTTE_WORLDTOWER }}
          DISCORD_MENTION_ID: ${{ secrets.DISCORD_MENTION_ID }}
        run: |
          python -u lotte_cancel_gyeongju_20260910.py

      - name: Save state
        if: always()
        shell: bash
        run: |
          set -e

          if [ ! -f "state_cancel_gyeongju_20260910.json" ]; then
            echo "No state file yet"
            exit 0
          fi

          git config user.name "github-actions[bot]"
          git config user.email "41898282+github-actions[bot]@users.noreply.github.com"

          git add "state_cancel_gyeongju_20260910.json"

          if git diff --cached --quiet; then
            echo "No state changes"
            exit 0
          fi

          git commit -m "Update Gyeongju cancel-ticket state"

          for attempt in 1 2 3; do
            git pull --rebase origin "${GITHUB_REF_NAME}" || true

            if git push origin "HEAD:${GITHUB_REF_NAME}"; then
              echo "State push complete"
              exit 0
            fi

            echo "Push retry $attempt/3"
            sleep 5
          done

          echo "State push failed after retries"
          exit 1
