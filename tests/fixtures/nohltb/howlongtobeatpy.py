"""Put this directory first on PYTHONPATH to make `import howlongtobeatpy` fail, whether or not the
real package is installed. The exporter treats that as "not installed" and skips HowLongToBeat,
so tests never reach the real, unofficial HowLongToBeat service."""
raise ImportError("howlongtobeatpy is hidden for tests")
