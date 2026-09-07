import httpx
import pytest

from app.main import app, capabilities, health, version


def test_operational_endpoints_are_attributable_and_fail_closed():
    assert {
        "/health",
        "/health/live",
        "/ready",
        "/readiness",
        "/health/ready",
        "/version",
        "/capabilities",
    }.issubset(app.openapi()["paths"])
    assert health()["service"] == "codestra-marketing"
    assert version()["service"] == "codestra-marketing"
    value = capabilities()
    assert value["business_writes_enabled"] is False
    assert value["live_advertising_enabled"] is False
    assert value["read_only_mode"] is True


def test_version_does_not_invent_runtime_attribution():
    value = version()
    assert value["git_sha"] == "unknown"
    assert value["image_digest"] == "unknown"
    assert value["release_id"] == "unknown"
    assert value["schema_version"] == "unknown"
    assert value["configuration_checksum"] == "unknown"


@pytest.mark.asyncio
async def test_operational_headers_and_content_type():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/health", headers={"X-Correlation-ID": "contract-id"})
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-correlation-id"] == "contract-id"
    assert response.headers["content-type"].startswith("application/json")


@pytest.mark.asyncio
async def test_metrics_are_private_and_not_in_openapi():
    assert "/metrics" not in app.openapi()["paths"]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/metrics")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_oversized_correlation_id_is_rejected_before_persistence():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/health", headers={"X-Correlation-ID": "x" * 129})
    assert response.status_code == 400
    assert response.json()["detail"] == "invalid_correlation_id"
