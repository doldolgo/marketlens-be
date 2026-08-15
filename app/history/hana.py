"""하나은행 고시환율(USD/KRW) 수집기 — 통일 환율의 원천.

김프 사이트들이 기준으로 삼는 "실환율"은 은행이 고시하는 **매매기준율**이다.
하나은행은 하루 1,300~2,000회(평균 약 44초 간격) 고시하며, 고시 시각이
초 단위로 찍혀 나온다. 과거 15개월 이상 조회 가능함을 실호출로 확인했다
(2026-08-13). 키·인증이 필요 없다.

엔드포인트
    POST https://www.kebhana.com/cms/rate/wpfxd651_01i_01.do  (HTML 조각 응답)

    body 파라미터
        ajax=true, inqKindCd=1, requestTarget=searchContentDiv
        inqStrDt=YYYYMMDD, tmpInpStrDt=YYYY-MM-DD   — 기준일
        pbldDvCd=0 → 그 기준일의 **최종 회차**
        pbldDvCd=1 → 1회차
        pbldDvCd=2 & pbldSqn=N → 특정 N 회차

주의: "기준일" 의 고시는 당일 아침부터 **다음날 아침(~07시 KST)까지** 이어진다.
즉 고시 시각의 날짜와 기준일이 다를 수 있다 — 저장은 항상 고시 시각 기준이다.

테스트 예시 (POST 라 브라우저 링크로는 안 되고 터미널에서):
    curl -s -X POST "https://www.kebhana.com/cms/rate/wpfxd651_01i_01.do" \
      -H "User-Agent: Mozilla/5.0" \
      -d "ajax=true&inqStrDt=20260813&tmpInpStrDt=2026-08-13&inqKindCd=1&requestTarget=searchContentDiv&pbldDvCd=0"
    → HTML 조각이 오고, 고시일시(초 단위)·회차와 USD 행 8번째 셀(매매기준율)을 파싱한다.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import httpx

from app.core.config import settings
from app.core.http import get_client, record_call

#: 한국 표준시 — 고시 시각은 KST 로 표기된다.
KST = timezone(timedelta(hours=9))

#: 은행 웹서버는 거래소 API 보다 느리므로 타임아웃을 넉넉히 잡는다.
_TIMEOUT = httpx.Timeout(15.0, connect=5.0)

#: 고시일시 + 회차. 예: "2026년06월10일</strong> <strong>08시24분28초 </strong><strong>(1회차)"
_PUBLISHED_RE = re.compile(
    r"(\d{4})년(\d{2})월(\d{2})일\s*</strong>\s*<strong>"
    r"(\d{2})시(\d{2})분(\d{2})초\s*</strong>\s*<strong>\((\d+)회차\)"
)

#: USD 행 다음에 오는 숫자 셀들. 8번째가 매매기준율이다.
_CELL_RE = re.compile(r'<td class="txtAr">([0-9.,]+)</td>')

DEFAULT_PACE = 0.35
RETRIES = 3
RETRY_WAIT = 2.0


@dataclass(slots=True)
class UsdKrwObservation:
    """고시 한 건 — 언제(초 단위), 얼마(매매기준율), 몇 회차."""

    ts: int  # 고시 시각 (epoch 초)
    rate: Decimal  # USD/KRW 매매기준율
    round_no: int  # 기준일 내 고시 회차
    basis_date: date  # 은행 기준일 (고시 시각의 날짜와 다를 수 있다)


class HanaParseError(RuntimeError):
    """응답 HTML 에서 고시 정보를 찾지 못했다 — 휴일이거나 형식 변경."""


def _parse(html: str, basis_date: date) -> UsdKrwObservation:
    """응답 HTML 조각에서 고시일시·회차·USD 매매기준율을 뽑는다."""
    published = _PUBLISHED_RE.search(html)
    if published is None:
        raise HanaParseError(f"{basis_date} 응답에 고시일시가 없습니다 (휴일?)")
    year, month, day, hour, minute, second, round_no = (
        int(g) for g in published.groups()
    )
    ts = int(
        datetime(year, month, day, hour, minute, second, tzinfo=KST).timestamp()
    )

    # USD 행을 찾아 그 행의 셀만 잘라 읽는다. 8번째 셀 = 매매기준율.
    usd_pos = html.find("goFluctuation('USD'")
    if usd_pos < 0:
        raise HanaParseError(f"{basis_date} 응답에 USD 행이 없습니다")
    row_html = html[usd_pos : html.find("</tr>", usd_pos)]
    cells = _CELL_RE.findall(row_html)
    if len(cells) < 8:
        raise HanaParseError(
            f"{basis_date} USD 행의 셀이 {len(cells)}개뿐입니다 (형식 변경?)"
        )
    rate = Decimal(cells[7].replace(",", ""))
    if rate <= 0:
        raise HanaParseError(f"{basis_date} 매매기준율이 0 이하입니다: {rate}")

    return UsdKrwObservation(
        ts=ts, rate=rate, round_no=round_no, basis_date=basis_date
    )


async def _post(basis_date: date, *, pbld_dv_cd: str, pbld_sqn: int | None) -> str:
    """고시 조회 폼을 POST 하고 HTML 조각을 받는다."""
    body = {
        "ajax": "true",
        "inqStrDt": basis_date.strftime("%Y%m%d"),
        "tmpInpStrDt": basis_date.strftime("%Y-%m-%d"),
        "inqKindCd": "1",
        "requestTarget": "searchContentDiv",
        "pbldDvCd": pbld_dv_cd,
    }
    if pbld_sqn is not None:
        body["pbldSqn"] = str(pbld_sqn)

    client = get_client()
    last_error: Exception | None = None
    for attempt in range(RETRIES):
        record_call("hana")
        try:
            response = await client.post(
                f"{settings.hana_base_url}/cms/rate/wpfxd651_01i_01.do",
                data=body,
                headers={
                    "User-Agent": "Mozilla/5.0",
                    "Referer": f"{settings.hana_base_url}/cms/rate/wpfxd651_01i.do",
                },
                timeout=_TIMEOUT,
            )
            response.raise_for_status()
            return response.text
        except (httpx.HTTPError,) as exc:
            last_error = exc
            await asyncio.sleep(RETRY_WAIT * (attempt + 1))
    raise last_error  # type: ignore[misc]


async def fetch_round(basis_date: date, round_no: int) -> UsdKrwObservation:
    """기준일의 특정 회차 고시 한 건."""
    html = await _post(basis_date, pbld_dv_cd="2", pbld_sqn=round_no)
    return _parse(html, basis_date)


async def fetch_final_round(basis_date: date) -> UsdKrwObservation:
    """기준일의 최종 회차 고시 — 회차 수 파악과 최신값 폴링에 쓴다."""
    html = await _post(basis_date, pbld_dv_cd="0", pbld_sqn=None)
    return _parse(html, basis_date)


async def fetch_latest() -> UsdKrwObservation:
    """지금 시점의 최신 고시. ``POST /refresh`` 가 환율을 얻는 통로다.

    오늘(KST)을 기준일로 최종 회차를 묻는다. 고시가 없는 날(이른 아침·주말·
    휴일)에도 은행이 **가장 최근 영업일의 최종 회차를 대신 돌려주므로** 한
    번만 물으면 된다. 2026-08-15(토·광복절)와 미래 날짜로 실호출해 확인했다 —
    둘 다 직전 영업일(8/14)의 1095회차가 돌아왔다.

    반환값의 ``basis_date`` 는 **요청한 날짜**라 실제 고시일과 다를 수 있다.
    실제 시각은 항상 응답에서 파싱한 ``ts`` 이므로 저장에는 문제가 없다.
    """
    return await fetch_final_round(datetime.now(tz=KST).date())


async def fetch_day_rounds(
    basis_date: date,
    *,
    stride: int = 1,
    pace: float = DEFAULT_PACE,
) -> list[UsdKrwObservation]:
    """기준일의 고시를 회차 순서대로 모두(또는 stride 간격으로) 가져온다.

    백필 전용 — 하루 1,300~2,000회차라 stride=1 이면 그만큼의 요청이 나간다.
    은행 서버에 부담을 주지 않도록 pace 로 간격을 강제한다.

    Returns:
        회차 오름차순 관측 목록. 휴일이면 빈 목록.
    """
    try:
        final = await fetch_final_round(basis_date)
    except HanaParseError:
        return []  # 휴일 — 이 기준일에는 고시가 없다

    observations: list[UsdKrwObservation] = []
    for round_no in range(1, final.round_no + 1, max(1, stride)):
        if round_no == final.round_no:
            observations.append(final)
            continue
        await asyncio.sleep(pace)
        try:
            observations.append(await fetch_round(basis_date, round_no))
        except HanaParseError:
            continue  # 개별 회차 파싱 실패는 건너뛴다 (한 건 손실 < 전체 중단)
    if observations and observations[-1].round_no != final.round_no:
        observations.append(final)
    return observations
