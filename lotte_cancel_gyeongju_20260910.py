#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import requests

LOTTE_API = "https://www.lottecinema.co.kr/LCWS/Ticketing/TicketingData.aspx"

SITE_NAME = "롯데시네마 월드타워"
CINEMA_ID = "1|0001|1016"
CINEMA_CODE = "1016"

TARGET_MOVIE = "경주기행"
TARGET_DATE = "2026-09-10"
TARGET_START = "19:30"
TARGET_END = "21:31"
TARGET_SCREEN = "14관"
TARGET_SCREEN_ID = "101614"
TARGET_REP_MOVIE_CODE = "24674"
TARGET_KIND = "GV"

STATE_FILE = Path("state_cancel_gyeongju_20260910.json")

CHECK_INTERVAL = float(os.getenv("CHECK_INTERVAL", "5"))
LOG_INTERVAL = 600
RUN_SECONDS = int(os.getenv("RUN_SECONDS", "19200"))

DISCORD_WEBHOOK = os.getenv("DISCORD_LOTTE_WORLDTOWER", "").strip()
DISCORD_MENTION_ID = os.getenv("DISCORD_MENTION_ID", "").strip()

KST = ZoneInfo("Asia/Seoul")

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/151.0.0.0 Safari/537.36"
)

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": UA,
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.lottecinema.co.kr/NLCHS/Ticketing",
    "Origin": "https://www.lottecinema.co.kr",
})


def log(message=""):
    now = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now} KST] {message}", flush=True)


def norm(value):
    return " ".join(str(value or "").split())


def compact(value):
    return re.sub(r"\s+", "", norm(value))


def lotte_post(payload):
    files = {
        "paramList": (
            None,
            json.dumps(payload, ensure_ascii=False),
        )
    }

    response = SESSION.post(
        LOTTE_API,
        files=files,
        timeout=15
    )

    response.raise_for_status()
    return response.json(), response


def fetch_target_rows():
    payload = {
        "MethodName": "GetPlaySequence",
        "channelType": "HO",
        "osType": "W",
        "osVersion": UA,
        "playDate": TARGET_DATE,
        "cinemaID": CINEMA_ID,
        "representationMovieCode": TARGET_REP_MOVIE_CODE,
    }

    data, response = lotte_post(payload)
    rows = []

    context_keys = (
        "MovieNameKR",
        "MovieName",
        "RepresentationMovieCode",
        "MovieCode",
        "AccompanyTypeCode",
        "AccompanyTypeNameKR",
    )

    def walk(value, inherited=None):
        inherited = dict(inherited or {})

        if isinstance(value, dict):
            context = dict(inherited)

            for key in context_keys:
                if key in value and norm(value.get(key)):
                    context[key] = value.get(key)

            start = norm(
                value.get("StartTime")
                or value.get("PlayStartTime")
                or value.get("StartTm")
            )

            screen = norm(
                value.get("ScreenNameKR")
                or value.get("ScreenName")
                or value.get("ScreenID")
            )

            if start and screen:
                row = dict(context)
                row.update(value)
                rows.append(row)

            for child in value.values():
                walk(child, context)

        elif isinstance(value, list):
            for child in value:
                walk(child, inherited)

    walk(data)
    return rows, response


def is_target(row):
    start = norm(
        row.get("StartTime")
        or row.get("PlayStartTime")
        or row.get("StartTm")
    )

    screen_name = norm(
        row.get("ScreenNameKR")
        or row.get("ScreenName")
    )

    screen_id = norm(
        row.get("ScreenID")
        or row.get("ScreenId")
        or row.get("ScreenCode")
    )

    movie_name = norm(
        row.get("MovieNameKR")
        or row.get("MovieName")
        or TARGET_MOVIE
    )

    rep_code = norm(
        row.get("RepresentationMovieCode")
        or TARGET_REP_MOVIE_CODE
    )

    event_code = norm(row.get("AccompanyTypeCode"))

    time_ok = start == TARGET_START
    screen_ok = (
        screen_id == TARGET_SCREEN_ID
        or compact(screen_name) == compact(TARGET_SCREEN)
    )
    movie_ok = (
        compact(movie_name) == compact(TARGET_MOVIE)
        or rep_code == TARGET_REP_MOVIE_CODE
    )
    gv_ok = event_code in {"", "40", "040", "40.0"}

    return time_ok and screen_ok and movie_ok and gv_ok


def find_target(rows):
    matches = [row for row in rows if is_target(row)]

    if not matches:
        return None

    def quality(row):
        keys = (
            "BookingSeatCount",
            "RemainSeatCount",
            "RemainingSeatCount",
            "IsBookingYN",
            "ScreenID",
            "EndTime",
            "AccompanyTypeCode",
            "MovieNameKR",
        )
        return sum(bool(norm(row.get(key))) for key in keys)

    return max(matches, key=quality)


def parse_int(value):
    text = norm(value).replace(",", "")

    if not text:
        return None

    match = re.search(r"-?\d+", text)

    if not match:
        return None

    try:
        return int(match.group(0))
    except ValueError:
        return None


def seat_snapshot(row, previous_remain=None):
    booking = norm(
        row.get("IsBookingYN")
        or row.get("BookingYN")
    ).upper()

    raw_remain = (
        row.get("BookingSeatCount")
        if row.get("BookingSeatCount") is not None
        else row.get("RemainSeatCount")
    )

    if raw_remain is None:
        raw_remain = row.get("RemainingSeatCount")

    remain = parse_int(raw_remain)

    if booking == "E":
        return 0, booking, "SOLD_OUT"

    if remain is not None:
        return max(0, remain), booking, "API_COUNT"

    # 좌석 숫자가 없는 Y 상태만으로는 취소표라고 판단하지 않는다.
    # 롯데 API가 매진 회차를 순간적으로 Y로 표시하는 경우의 오알림 방지.
    if booking in {"Y", "YES", "TRUE", "1"}:
        return None, booking, "OPEN_WITHOUT_COUNT"

    return None, booking, "UNKNOWN"


def load_state():
    if not STATE_FILE.exists():
        return {}

    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_state(state):
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )


def booking_url():
    params = {
        "link_channelCode": "naver",
        "link_cinemaCode": CINEMA_CODE,
        "link_date": TARGET_DATE,
        "link_movieCd": TARGET_REP_MOVIE_CODE,
        "link_screenId": TARGET_SCREEN_ID,
        "link_time": TARGET_START,
    }

    return (
        "https://www.lottecinema.co.kr/NLCMW/ticketing?"
        + urlencode(params)
    )


def discord_post(content):
    if not DISCORD_WEBHOOK:
        raise RuntimeError(
            "DISCORD_LOTTE_WORLDTOWER Secret이 비어 있습니다."
        )

    if not DISCORD_MENTION_ID:
        raise RuntimeError(
            "DISCORD_MENTION_ID Secret이 비어 있습니다."
        )

    payload = {
        "content": content,
        "flags": 4,
        "allowed_mentions": {
            "parse": [],
            "users": [DISCORD_MENTION_ID],
        },
    }

    response = requests.post(
        DISCORD_WEBHOOK,
        json=payload,
        timeout=15
    )
    response.raise_for_status()


def cancel_alert_text():
    url = booking_url()

    return "\n".join([
        f"<@{DISCORD_MENTION_ID}>",
        "**🎟️ 취소표가 생겼습니다**",
        f"**🎬 {SITE_NAME} · {TARGET_KIND}**",
        f"**📅 {TARGET_DATE}**",
        (
            f"**[🎟 {TARGET_START}–{TARGET_END} · "
            f"{TARGET_MOVIE} · {TARGET_SCREEN}]({url})**"
        ),
    ])


def target_finished():
    now = datetime.now(KST)

    target_end = datetime.strptime(
        f"{TARGET_DATE} {TARGET_END}",
        "%Y-%m-%d %H:%M"
    ).replace(tzinfo=KST)

    return now > target_end


def main():
    log("=" * 70)
    log("LOTTE CINEMA WORLDTOWER GYEONGJU CANCEL-TICKET MONITOR")
    log("=" * 70)

    log(
        f"TARGET: {TARGET_MOVIE} / "
        f"{TARGET_DATE} / "
        f"{TARGET_START}-{TARGET_END} / "
        f"{TARGET_SCREEN} / "
        f"{TARGET_KIND}"
    )

    log(f"CHECK: {CHECK_INTERVAL:g}초마다 실제 감지")
    log("LOG: 정상 상태는 10분마다 표시")
    log("ALERT: 잔여석 증가 시 즉시 Discord 알림")
    log("RULE: 0→1 / 1→2 / 2→4 = ALERT")
    log("RULE: 4→2 / 2→2 = NO ALERT")
    log("=" * 70)

    state = load_state()
    started = time.time()
    last_status_log = 0.0

    while time.time() - started < RUN_SECONDS:
        if target_finished():
            log("대상 회차 종료 시각이 지나 감시를 종료합니다.")
            break

        cycle_started = time.time()

        try:
            rows, response = fetch_target_rows()
            target = find_target(rows)

            if target is None:
                log(
                    f"⚠️ TARGET_NOT_FOUND / "
                    f"HTTP={response.status_code} / "
                    f"ROWS={len(rows)}"
                )
            else:
                previous = None

                if (
                    state.get("initialized")
                    and state.get("last_remain") is not None
                ):
                    previous = int(state["last_remain"])

                current, booking, count_source = seat_snapshot(
                    target,
                    previous_remain=previous
                )

                if current is None:
                    log(
                        "⚠️ TARGET=FOUND / "
                        f"IsBookingYN={booking or '(blank)'} / "
                        "REMAIN=UNKNOWN"
                    )

                elif not state.get("initialized"):
                    state = {
                        "initialized": True,
                        "target_movie": TARGET_MOVIE,
                        "target_date": TARGET_DATE,
                        "target_start": TARGET_START,
                        "target_end": TARGET_END,
                        "target_screen": TARGET_SCREEN,
                        "last_remain": current,
                        "last_booking": booking,
                        "count_source": count_source,
                        "updated_at_kst": datetime.now(KST).isoformat(
                            timespec="seconds"
                        ),
                    }

                    save_state(state)

                    log(
                        "✅ BASELINE SET: "
                        f"잔여 {current}석 / "
                        f"IsBookingYN={booking or '(blank)'} / "
                        f"{count_source}"
                    )

                    last_status_log = time.time()

                else:
                    previous = int(state.get("last_remain", 0))
                    increased = current > previous

                    if increased:
                        log(
                            "🎟️ 취소표 감지: "
                            f"잔여 {previous}→{current}석"
                        )

                        discord_post(cancel_alert_text())

                        log("✅ Discord 취소표 알림 전송 완료")
                        last_status_log = time.time()

                    elif time.time() - last_status_log >= LOG_INTERVAL:
                        log(
                            "정상 감시중 · "
                            f"잔여 {current}석 · "
                            f"{CHECK_INTERVAL:g}초 간격 확인중"
                        )
                        last_status_log = time.time()

                    state.update({
                        "last_remain": current,
                        "last_booking": booking,
                        "count_source": count_source,
                        "updated_at_kst": datetime.now(KST).isoformat(
                            timespec="seconds"
                        ),
                    })
                    save_state(state)

        except Exception as error:
            log(
                f"❌ ERROR: "
                f"{type(error).__name__}: "
                f"{error}"
            )

        elapsed = time.time() - cycle_started
        sleep_for = max(0.0, CHECK_INTERVAL - elapsed)

        if sleep_for:
            time.sleep(sleep_for)

    log("MONITOR FINISHED")


if __name__ == "__main__":
    main()
