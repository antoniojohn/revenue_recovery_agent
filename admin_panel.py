"""
Admin settings panel: a small, separately-run Flask app for viewing and
editing the business policy boundaries that decide.py now reads from
agent/settings_store.py, instead of from hardcoded constants.

This is deliberately its own process/port from webhook_server.py: a
webhook receiver has one narrow job (verify + log + resolve), and
bolting an admin UI onto the same Flask app would blur that boundary
and grow its attack surface for no operational benefit. Two small,
single-purpose Flask apps are easier to reason about, deploy, and lock
down independently than one that does both.

Auth: HTTP Basic Auth against ADMIN_USERNAME/ADMIN_PASSWORD from the
environment. This is not meant to be a production-grade auth system -
for a real deployment, put this behind your cloud provider's IAM / SSO
/ VPN, the same way you would for any internal admin tool. It exists
here so the panel isn't wide open to anyone who finds the URL, matching
the "never trust an unauthenticated write" principle webhook_server.py
already applies to inbound Razorpay events.

Run: python agent/admin_panel.py
Or, in Docker: see docker-compose.yml (service: admin).
"""

import os
from functools import wraps

from dotenv import load_dotenv
from flask import Flask, request, Response

from agent import settings_store, decide

load_dotenv()

app = Flask(__name__)

RETRY_CAUSES = [cause for cause, action in decide.ACTION_MAP.items() if action.startswith("RETRY")]


def _check_auth(username: str, password: str) -> bool:
    expected_user = os.getenv("ADMIN_USERNAME", "admin")
    expected_pass = os.getenv("ADMIN_PASSWORD")
    if not expected_pass:
        # Fail closed, same philosophy as webhook_server's missing-secret
        # check - an admin panel with no configured password must never
        # silently allow access.
        return False
    return username == expected_user and password == expected_pass


def require_auth(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        auth = request.authorization
        if not auth or not _check_auth(auth.username, auth.password):
            return Response(
                "Authentication required.", 401,
                {"WWW-Authenticate": 'Basic realm="RecoverX Admin"'},
            )
        return view(*args, **kwargs)
    return wrapped


def _render_page(message: str = "") -> str:
    settings = settings_store.get_all_settings()
    min_retry = settings["min_retry_amount"]
    max_retries_meta = settings["max_retries"]
    max_retries = max_retries_meta["value"]

    rows = "".join(
        f"""
        <tr>
          <td>{cause}</td>
          <td>{decide.ACTION_MAP.get(cause, 'ESCALATE')}</td>
          <td>
            <form method="post" action="/update-max-retries" style="display:flex; gap:8px;">
              <input type="hidden" name="cause" value="{cause}">
              <input type="number" name="max_attempts" min="0" value="{max_retries.get(cause, 0)}" style="width:70px;">
              <button type="submit">Save</button>
            </form>
          </td>
        </tr>"""
        for cause in RETRY_CAUSES
    )

    banner = f'<p style="color:#2743D6;font-weight:600;">{message}</p>' if message else ""

    return f"""
    <!DOCTYPE html>
    <html>
    <head>
      <title>RecoverX — Policy Settings</title>
      <style>
        body {{ font-family: system-ui, sans-serif; max-width: 720px; margin: 48px auto; padding: 0 20px; color:#1a1a2e; }}
        h1 {{ font-size: 20px; }}
        h2 {{ font-size: 16px; margin-top: 32px; }}
        table {{ border-collapse: collapse; width: 100%; margin-top: 16px; }}
        th, td {{ text-align: left; padding: 8px 12px; border-bottom: 1px solid #eee; font-size: 14px; }}
        input[type=number] {{ padding: 6px; border-radius: 6px; border: 1px solid #ccc; }}
        button {{ background:#3B5BFE; color:#fff; border:none; padding: 6px 14px; border-radius: 6px; cursor:pointer; font-weight:600; }}
        button:hover {{ background:#2743D6; }}
        .meta {{ font-size:12px; color:#777; }}
        .note {{ background:#FFF6E9; border:1px solid #F79009; border-radius:8px; padding:12px; font-size:13px; margin-top:24px; }}
        code {{ background:#f0f0f5; padding:2px 6px; border-radius:4px; }}
      </style>
    </head>
    <body>
      <h1>RecoverX — Dynamic Policy Settings</h1>
      {banner}

      <form method="post" action="/update-min-retry-amount">
        <label>Minimum retry amount (₹) — retries below this are auto-escalated instead:</label><br>
        <input type="number" name="min_retry_amount" min="0" value="{min_retry['value']}" style="margin-top:8px;">
        <button type="submit">Save</button>
      </form>
      <p class="meta">Last changed: {min_retry['updated_at'] or 'never (default)'} by {min_retry['updated_by'] or 'system'}</p>

      <h2>Per-cause retry caps</h2>
      <table>
        <tr><th>Cause</th><th>Action</th><th>Max attempts</th></tr>
        {rows}
      </table>
      <p class="meta">Last changed: {max_retries_meta['updated_at'] or 'never (default)'} by {max_retries_meta['updated_by'] or 'system'}</p>

      <div class="note">
        Changes take effect immediately for the next <code>decide.choose_action()</code>
        call — no restart or redeploy required. Every change here is appended to
        <code>logs/settings_audit_log.json</code> with who changed it and the
        old/new value, the same audit-trail principle as every payment decision
        in this pipeline.
      </div>
    </body>
    </html>
    """


@app.route("/", methods=["GET"])
@require_auth
def index():
    return _render_page()


@app.route("/update-min-retry-amount", methods=["POST"])
@require_auth
def update_min_retry_amount():
    try:
        new_value = int(request.form["min_retry_amount"])
        settings_store.update_min_retry_amount(new_value, updated_by=request.authorization.username)
        message = f"Minimum retry amount updated to ₹{new_value}."
    except (KeyError, ValueError) as e:
        message = f"Update rejected: {e}"
    return _render_page(message)


@app.route("/update-max-retries", methods=["POST"])
@require_auth
def update_max_retries():
    try:
        cause = request.form["cause"]
        new_value = int(request.form["max_attempts"])
        settings_store.update_max_retries(cause, new_value, updated_by=request.authorization.username)
        message = f"Max retries for {cause} updated to {new_value}."
    except (KeyError, ValueError) as e:
        message = f"Update rejected: {e}"
    return _render_page(message)


@app.route("/health", methods=["GET"])
def health():
    return {"status": "ok"}, 200


if __name__ == "__main__":
    if not os.getenv("ADMIN_PASSWORD"):
        print(
            "[admin_panel] WARNING: ADMIN_PASSWORD is not set. Every request "
            "will be rejected with 401 until it is configured."
        )
    os.makedirs("logs", exist_ok=True)
    os.makedirs("instance", exist_ok=True)
    app.run(port=5001, debug=False)
