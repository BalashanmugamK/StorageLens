OBSERVATION_PERIOD_DAYS = 30
DAYS_PER_YEAR = 365


def calculate_access_frequency(access_count: int) -> float:
    """Return average downloads per day over the last 30 days."""
    return access_count / OBSERVATION_PERIOD_DAYS


def calculate_expected_annual_downloads(access_frequency: float) -> float:
    """Extrapolate 30-day access frequency over one year."""
    return access_frequency * DAYS_PER_YEAR


def calculate_expected_retrieved_gb(
    expected_annual_downloads: float,
    file_size_bytes: int
) -> float:
    """Assume every download retrieves the complete document."""
    file_size_gb = file_size_bytes / 1_000_000_000
    return expected_annual_downloads * file_size_gb
