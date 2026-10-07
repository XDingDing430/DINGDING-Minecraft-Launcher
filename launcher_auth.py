"""Microsoft authentication; call from a worker, never from the Qt UI thread."""

import time

import msal
import requests

from launcher_core import Account, LauncherError

AUTHORITY = "https://login.microsoftonline.com/consumers"
SCOPES = ["XboxLive.signin"]


def _request_json(session, method, url, **kwargs):
    try:
        response = session.request(method, url, timeout=(10, 30), **kwargs)
        if response.status_code != 200:
            if "xsts" in url:
                try:
                    code = response.json().get("XErr")
                except ValueError:
                    code = None
                messages = {2148916233: "该 Microsoft 账户尚未创建 Xbox 资料，请先完成 Xbox 账户设置。",
                            2148916238: "该账户需要在 Xbox 家庭设置中完成家长授权。"}
                if code in messages:
                    raise LauncherError(messages[code])
            if "minecraft/profile" in url and response.status_code == 404:
                raise LauncherError("该账户没有可用的 Minecraft Java 玩家资料，请确认已拥有 Java 版并创建角色。")
            raise LauncherError(f"认证服务请求失败（HTTP {response.status_code}），请检查账户和网络后重试。")
        data = response.json()
        if not isinstance(data, dict):
            raise LauncherError("认证服务返回的数据格式无效。")
        return data
    except (requests.RequestException, ValueError) as exc:
        if isinstance(exc, LauncherError):
            raise
        raise LauncherError("认证服务连接失败或响应无效，请检查网络后重试。") from exc


def login_microsoft(client_id: str, emit_log) -> Account:
    if not client_id.strip():
        raise LauncherError("启动器开发者尚未配置 Microsoft 应用 Client ID。")
    emit_log("正在打开 Microsoft 登录页面…")
    try:
        app = msal.PublicClientApplication(client_id.strip(), authority=AUTHORITY, timeout=(10, 30))
        result = app.acquire_token_interactive(scopes=SCOPES, timeout=180)
    except Exception as exc:
        raise LauncherError("Microsoft 登录连接失败，请检查网络、应用 Client ID 或重新登录。") from exc
    token = result.get("access_token")
    if not token:
        # Do not dump authentication responses or credential-bearing URLs into logs.
        error = result.get("error", "unknown_error")
        raise LauncherError(f"Microsoft 登录未完成（{error}）。请检查应用配置或重新登录。")
    with requests.Session() as session:
        emit_log("正在认证 Xbox Live…")
        xbox = _request_json(session, "POST", "https://user.auth.xboxlive.com/user/authenticate", json={
            "Properties": {"AuthMethod": "RPS", "SiteName": "user.auth.xboxlive.com", "RpsTicket": "d=" + token},
            "RelyingParty": "http://auth.xboxlive.com", "TokenType": "JWT"})
        if not xbox.get("Token"):
            raise LauncherError("Xbox Live 没有返回 Token。")
        emit_log("正在获取 XSTS 授权…")
        xsts = _request_json(session, "POST", "https://xsts.auth.xboxlive.com/xsts/authorize", json={
            "Properties": {"SandboxId": "RETAIL", "UserTokens": [xbox["Token"]]},
            "RelyingParty": "rp://api.minecraftservices.com/", "TokenType": "JWT"})
        users = xsts.get("DisplayClaims", {}).get("xui", [])
        if not xsts.get("Token") or not users or not users[0].get("uhs"):
            raise LauncherError("XSTS 返回的账户信息不完整。")
        emit_log("正在获取 Minecraft 登录凭据…")
        minecraft = _request_json(session, "POST", "https://api.minecraftservices.com/authentication/login_with_xbox",
                                  json={"identityToken": f"XBL3.0 x={users[0]['uhs']};{xsts['Token']}"})
        minecraft_token = minecraft.get("access_token")
        if not minecraft_token:
            raise LauncherError("Minecraft 认证没有返回 Token。")
        profile = _request_json(session, "GET", "https://api.minecraftservices.com/minecraft/profile",
                                headers={"Authorization": "Bearer " + minecraft_token})
        if not profile.get("name") or not profile.get("id"):
            raise LauncherError("Minecraft 玩家资料不完整。")
        return Account(profile["name"], profile["id"], minecraft_token, "microsoft",
                       str(users[0].get("xid", "")), time.time() + int(minecraft.get("expires_in", 86400)))
