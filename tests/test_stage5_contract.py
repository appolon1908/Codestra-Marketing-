from app.main import LIVE_ADVERTISING_ENABLED, META_READ_SYNC_ENABLED, app
from app.providers.meta_read import MetaReadClient


def test_canonical_stage5_routes_are_registered_without_legacy_drift():
    paths = set(app.openapi()["paths"])
    required = {
        "/v1/marketing/capabilities",
        "/v1/marketing/campaigns",
        "/v1/marketing/campaigns/{campaign_id}",
        "/v1/marketing/campaigns/{campaign_id}/reject",
        "/v1/marketing/campaigns/{campaign_id}/pause",
        "/v1/marketing/campaigns/{campaign_id}/resume",
        "/v1/marketing/campaigns/{campaign_id}/submit-for-approval",
        "/v1/marketing/campaigns/{campaign_id}/approve",
        "/v1/marketing/campaigns/{campaign_id}/activate",
        "/v1/marketing/operations/{operation_id}",
        "/v1/marketing/audiences",
        "/v1/marketing/audiences/{audience_id}",
        "/v1/marketing/creatives",
        "/v1/marketing/creatives/{creative_id}",
        "/v1/marketing/creatives/{creative_id}/submit-for-approval",
        "/v1/marketing/creatives/{creative_id}/approve",
        "/v1/marketing/creatives/{creative_id}/reject",
        "/v1/marketing/providers/meta/accounts/{ad_account_id}/campaigns",
    }
    assert required.issubset(paths)
    assert "/v1/campaigns" not in paths
    assert "/v1/campaigns/{campaign_id}/request-approval" not in paths


def test_all_external_effect_capabilities_default_off():
    assert LIVE_ADVERTISING_ENABLED is False
    assert META_READ_SYNC_ENABLED is False


def test_meta_read_adapter_has_no_write_methods():
    forbidden = {
        "create_campaign",
        "update_campaign",
        "activate_campaign",
        "delete_campaign",
        "post",
        "put",
        "patch",
        "delete",
    }
    assert forbidden.isdisjoint(set(dir(MetaReadClient())))
