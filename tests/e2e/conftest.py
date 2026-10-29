import pytest
from sse_starlette.sse import AppStatus


@pytest.fixture(autouse=True)
def isolate_sse_shutdown_event():
    # Each TestClient owns a new loop; the SSE library retains its exit event globally.
    AppStatus.should_exit_event = None
    yield
    AppStatus.should_exit_event = None
