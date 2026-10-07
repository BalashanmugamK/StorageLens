OBSERVATION_PERIOD_DAYS = 30
DAYS_PER_YEAR = 365


def calculate_access_frequency(access_count: int) -> float:
    """Return average downloads per day over the last 30 days."""
    return access_count / OBSERVATION_PERIOD_DAYS


def calculate_recency_weight(
    days_since_last_access: float | None,
    half_life_days: float,
) -> float:
    """
    Exponential recency attenuation for the access forecast.

    A document whose last observed download is stale inside the
    30-day window is less likely to keep that download rate for a
    full year, so its observed frequency is scaled down:

        weight = 0.5 ** (days_since_last_access / half_life_days)

    The weight is 1.0 at half_life_days == 0 and when the document
    has no observed downloads (None stays 1.0 so the forecast
    remains frequency-driven, and frequency is already 0 there).
    """

    if days_since_last_access is None:
        return 1.0

    if half_life_days <= 0:
        return 1.0

    if days_since_last_access <= 0:
        return 1.0

    return 0.5 ** (days_since_last_access / half_life_days)


def calculate_expected_annual_downloads(
    access_frequency: float,
    days_since_last_access: float | None = None,
    recency_half_life_days: float | None = None,
) -> float:
    """
    Extrapolate 30-day access frequency over one year,
    optionally attenuated by recency.

    Without recency input the forecast is the plain
    frequency x 365 extrapolation used by the original model.
    """

    annual = access_frequency * DAYS_PER_YEAR

    if recency_half_life_days is None:
        return annual

    weight = calculate_recency_weight(
        days_since_last_access,
        recency_half_life_days,
    )

    return annual * weight


def calculate_expected_retrieved_gb(
    expected_annual_downloads: float,
    file_size_bytes: int
) -> float:
    """Assume every download retrieves the complete document."""
    file_size_gb = file_size_bytes / 1_000_000_000
    return expected_annual_downloads * file_size_gb