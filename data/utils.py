from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))

def get_now_kst() -> datetime:
    """현재 한국 시간을 반환합니다."""
    return datetime.now(KST)

def format_kst(dt: datetime, fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    """datetime 객체를 KST 형식으로 포맷팅합니다."""
    if dt.tzinfo is None:
        # 시간대가 없는 경우 KST로 가정하거나 변환 (시스템 설정에 따라 다를 수 있음)
        # 여기서는 단순히 KST로 변환하여 출력
        return dt.replace(tzinfo=timezone.utc).astimezone(KST).strftime(fmt)
    return dt.astimezone(KST).strftime(fmt)
