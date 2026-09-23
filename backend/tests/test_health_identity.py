"""`/api/health` is an IDENTITY endpoint, not only a liveness probe.

Port 8000 is the documented default and a contested one: during an end-to-end
pass an unrelated local Python application was holding it, so the dashboard —
built with `NEXT_PUBLIC_API_BASE=http://localhost:8000` — was pointed at a
stranger's API. Every request still completed at the transport level, and the
only thing the operator saw was "Login failed".

The dashboard now refuses to be silent about that: before login it calls this
endpoint and requires `service == "sentinel-vision-backend"` (see
`frontend/lib/api.ts`, `checkBackendIdentity`). That makes the string below a
cross-language contract rather than a decorative field, and renaming it would
make every correctly configured dashboard declare the backend an impostor.

The frontend's own unit tests cover the classification; this covers the half
of the contract that lives in Python.
"""


SERVICE_NAME = "sentinel-vision-backend"


class TestHealthIdentity:
    def test_health_is_reachable_without_authentication(self, client):
        """The preflight runs before anyone has a token; requiring one would
        make a misconfigured base URL indistinguishable from a logged-out
        session — exactly the confusion being removed."""
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
        """Unauthenticated and world-reachable, so its body is a disclosure
        surface: version strings, hostnames or database paths here would be
        free reconnaissance."""
        assert set(client.get("/api/health").json()) == {"ok", "service"}
