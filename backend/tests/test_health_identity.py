"""/api/health identifies the service, it isn't only a liveness probe.

Port 8000 is contested: during an end-to-end run some unrelated local Python
app held it, the dashboard talked to that, and the operator only saw "Login
failed". Now the dashboard checks service == "sentinel-vision-backend"
before login (frontend/lib/api.ts checkBackendIdentity), so that string is a
contract between the two. Frontend tests cover the other half.
"""


SERVICE_NAME = "sentinel-vision-backend"


class TestHealthIdentity:
    def test_health_is_reachable_without_authentication(self, client):
        """Runs before anyone has a token; requiring one would make a wrong
        base URL look like a logged-out session."""
        assert client.get("/api/health").status_code == 200

    def test_it_names_the_service(self, client):
        body = client.get("/api/health").json()
        assert body["service"] == SERVICE_NAME, (
            "the dashboard compares this exact string to decide whether it is "
            "talking to SENTINEL or to whatever else grabbed the port"
        )

    def test_it_still_reports_liveness(self, client):
        assert client.get("/api/health").json()["ok"] is True

    def test_it_leaks_nothing_beyond_identity_and_liveness(self, client):
        """Public, so nothing useful for recon in the body (versions, hosts,
        DB paths)."""
        assert set(client.get("/api/health").json()) == {"ok", "service"}
