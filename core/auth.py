# -*- coding: utf-8 -*-
"""账户认证：离线模式 / 微软正版 OAuth / 外置登录（Authlib-Injector）

登录结果统一为 dict：
  {"type": ..., "username": ..., "uuid": ..., "token": ...}

v1.4 修复：
- 外置登录（LittleSkin 等皮肤站）真正走 Yggdrasil /authserver/authenticate，
  拿到真实的 accessToken 与角色 UUID，并用正确的
  -Dauthlibinjector.yggdrasil.prefetched（Base64 会话 JSON）注入，
  不会再出现"登录了但进服务器显示离线账号"
- 微软登录的 refresh_token 会持久化，启动前自动续期
- authlib-injector.jar 下载带校验，坏文件不会留下
"""
import base64
import json
import os
import re
import time
import uuid

import requests
import minecraft_launcher_lib.microsoft_account as ms

# 微软官方 Minecraft Launcher 的 Client ID（社区通行做法）
CLIENT_ID = "00000000402b5328"
REDIRECT_URI = "https://login.live.com/oauth20_desktop.srf"

AUTHLIB_INJECTOR_META = "https://authlib-injector.yushi.moe/artifact/latest.json"

USER_AGENT = "XueYuanLauncher/1.4.0 (Minecraft Java Launcher)"
YGGDRASIL_AGENT = {"name": "Minecraft", "version": 1}

# 游戏内部用 StringUtil.isValidPlayerName 校验用户名：
# 只允许 ASCII 字母 / 数字 / 下划线，长度最多 16。
# 名字里带中文、空格或符号时，客户端能启动，但进世界（内置服务器）会立刻断开：
#   Internal Exception: java.lang.IllegalStateException: Invalid characters in username
OFFLINE_NAME_RE = re.compile(r"^[A-Za-z0-9_]{1,16}$")


def check_offline_name(name: str) -> str:
    """校验离线用户名，返回清洗后的名字"""
    name = (name or "").strip()
    if not name:
        raise AuthError("离线用户名不能为空")
    if len(name) > 16:
        raise AuthError("离线用户名最多 16 个字符")
    if not OFFLINE_NAME_RE.match(name):
        raise AuthError(
            "离线用户名只能用英文字母、数字和下划线（例如 Steve、Alex_01）。"
            "中文 / 空格 / 符号会让游戏在进入世界时断开并提示 "
            "Invalid characters in username。")
    return name


class AuthError(Exception):
    pass


def _headers():
    return {"User-Agent": USER_AGENT, "Accept": "application/json"}


def _dashed_uuid(value: str) -> str:
    """Yggdrasil 返回的是 32 位无连字符 UUID，这里统一成带连字符格式"""
    text = (value or "").strip().replace("-", "")
    if len(text) == 32:
        try:
            return str(uuid.UUID(text))
        except ValueError:
            return value
    return value or ""


class AuthManager:
    def __init__(self, config):
        self.config = config
        self._pending_ms = None  # {"state":..., "verifier":...}

    # ------------------------------------------------------------ 当前登录
    def current(self) -> dict:
        return {
            "type": self.config.get("auth_type", "offline"),
            "data": self.config.get("auth_data", {}) or {},
        }

    def _save(self, auth_type, data):
        self.config.set("auth_type", auth_type)
        self.config.set("auth_data", data)

    # ------------------------------------------------------------ 离线模式
    def offline_login(self, name: str) -> dict:
        name = check_offline_name(name)
        info = {
            "username": name,
            "uuid": str(uuid.uuid3(uuid.NAMESPACE_URL, "offline:" + name)),
            "token": "0",
        }
        self._save("offline", info)
        return info

    # ------------------------------------------------------------ 微软正版
    def start_microsoft_login(self) -> str:
        """生成授权 URL，返回给用户在浏览器打开"""
        url, state, verifier = ms.get_secure_login_data(CLIENT_ID, REDIRECT_URI)
        self._pending_ms = {"state": state, "verifier": verifier}
        return url

    def complete_microsoft_login(self, callback_url: str) -> dict:
        if not self._pending_ms:
            raise AuthError("请先点「微软登录」生成授权链接")
        try:
            auth_code = ms.parse_auth_code_url(callback_url, self._pending_ms["state"])
        except Exception as e:  # noqa: BLE001
            raise AuthError(f"授权回调地址无效或已过期：{e}")
        verifier = self._pending_ms["verifier"]
        self._pending_ms = None
        return self._finish_microsoft(auth_code, verifier)

    def _finish_microsoft(self, auth_code, verifier=None) -> dict:
        try:
            login = ms.complete_login(CLIENT_ID, None, REDIRECT_URI, auth_code, verifier)
            return self._response_to_info(login)
        except Exception as e:  # noqa: BLE001
            raise AuthError(f"微软登录失败：{e}")

    def refresh_microsoft(self) -> dict:
        """用 refresh_token 静默续期（每次启动前调用）"""
        data = self.config.get("auth_data", {}) or {}
        refresh = data.get("refresh_token")
        if not refresh:
            raise AuthError("没有可用的刷新令牌")
        try:
            login = ms.complete_refresh(CLIENT_ID, None, REDIRECT_URI, refresh)
        except Exception as e:  # noqa: BLE001
            raise AuthError(f"刷新令牌失败，请重新登录：{e}")
        try:
            return self._response_to_info(login)
        except AuthError:
            raise
        except Exception as e:  # noqa: BLE001
            raise AuthError(f"刷新登录状态失败：{e}")

    def _response_to_info(self, login: dict) -> dict:
        info = {
            "username": login.get("name") or login.get("username", ""),
            "uuid": _dashed_uuid(login.get("id", "")),
            "token": login.get("access_token", ""),
            "refresh_token": login.get("refresh_token", "") or "",
        }
        if not info["username"] or not info["uuid"]:
            raise AuthError("微软账号响应缺少用户名或 UUID，请重新登录")
        self._save("microsoft", info)
        return info

    # ------------------------------------------------------------ 外置登录
    def authlib_login(self, username: str, password: str, server_url: str) -> dict:
        """Yggdrasil 外置登录：账号密码 → accessToken + 角色 UUID"""
        name = (username or "").strip()
        pwd = password or ""
        server_url = (server_url or "").strip().rstrip("/")
        if not name:
            raise AuthError("外置登录用户名不能为空")
        if not pwd:
            raise AuthError("外置登录需要填写皮肤站密码")
        if not server_url.startswith("http"):
            raise AuthError("外置登录 API 地址需以 http(s):// 开头，"
                            "例如 https://littleskin.cn/api/yggdrasil")

        # 1. 先确认地址指向一个 Yggdrasil API（返回里带 meta 字段）
        try:
            meta_resp = requests.get(server_url, timeout=15, headers=_headers())
        except Exception as e:  # noqa: BLE001
            raise AuthError(f"无法访问外置登录服务器 {server_url}：{e}")
        try:
            meta = meta_resp.json()
        except Exception:
            meta = None
        if not isinstance(meta, dict) or "meta" not in meta:
            raise AuthError("这个地址不是有效的 Yggdrasil API（缺少 meta 字段）。"
                            "请检查是否漏了 /api/yggdrasil 之类的后缀")

        # 2. 用账号密码换取 accessToken 和角色信息
        client_token = str(uuid.uuid4())
        payload = {
            "agent": YGGDRASIL_AGENT,
            "username": name,
            "password": pwd,
            "clientToken": client_token,
            "requestUser": True,
        }
        try:
            r = requests.post(server_url + "/authserver/authenticate", json=payload,
                              timeout=20, headers=_headers())
        except Exception as e:  # noqa: BLE001
            raise AuthError(f"连接外置登录服务器失败：{e}")

        try:
            body = r.json()
        except Exception:
            body = {}
        if r.status_code >= 400 or not isinstance(body, dict) or "accessToken" not in body:
            message = ""
            if isinstance(body, dict):
                message = body.get("errorMessage") or body.get("error") or ""
            if r.status_code in (401, 403) or "Invalid credentials" in str(message):
                raise AuthError("外置登录失败：账号或密码错误")
            raise AuthError(f"外置登录失败（HTTP {r.status_code}）：{message or r.text[:200]}")

        session = body
        session.setdefault("clientToken", client_token)

        profile = session.get("selectedProfile") or {}
        if not profile:
            profiles = session.get("availableProfiles") or []
            if profiles:
                profile = profiles[0]
            else:
                raise AuthError("登录成功，但这个账号还没有创建游戏角色，"
                                "请先到皮肤站创建一个角色")
            session["selectedProfile"] = profile

        profile_id = _dashed_uuid(profile.get("id", ""))
        if not profile_id:
            raise AuthError("外置登录响应里缺少角色 UUID，请换个皮肤站或稍后重试")

        info = {
            "username": profile.get("name") or name,
            "uuid": profile_id,
            "token": session.get("accessToken", ""),
            "client_token": session.get("clientToken", client_token),
            "server": server_url,
            "session": session,
        }
        if not OFFLINE_NAME_RE.match(info["username"] or ""):
            info["warning"] = ("这个皮肤站角色名里含非 ASCII 字符，"
                               "单人会进不去世界（Invalid characters in username），"
                               "多人服务器能否使用取决于服务端。")
        self._save("authlib", info)
        return info

    def authlib_refresh(self) -> dict:
        """启动前续期外置登录 token；失败时沿用旧 token（很多皮肤站 token 长期有效）"""
        data = dict(self.config.get("auth_data", {}) or {})
        server = (data.get("server") or "").rstrip("/")
        session = dict(data.get("session") or {})
        token = session.get("accessToken") or data.get("token", "")
        client_token = session.get("clientToken") or data.get("client_token", "")
        if not server or not token:
            raise AuthError("外置登录状态不完整，请重新登录")

        payload = {
            "accessToken": token,
            "clientToken": client_token,
            "requestUser": True,
        }
        try:
            r = requests.post(server + "/authserver/refresh", json=payload,
                              timeout=15, headers=_headers())
            if r.status_code == 200:
                fresh = r.json()
                if isinstance(fresh, dict) and fresh.get("accessToken"):
                    session.update({
                        "accessToken": fresh.get("accessToken"),
                        "clientToken": fresh.get("clientToken", client_token),
                    })
                    if fresh.get("selectedProfile"):
                        session["selectedProfile"] = fresh["selectedProfile"]
                    data.update({
                        "token": session["accessToken"],
                        "client_token": session.get("clientToken", client_token),
                        "session": session,
                        "username": (session.get("selectedProfile") or {}).get("name") or data.get("username"),
                        "uuid": _dashed_uuid((session.get("selectedProfile") or {}).get("id"))
                                or data.get("uuid"),
                    })
                    self.config.set("auth_data", data)
        except Exception:
            pass  # 续期失败不阻塞启动，继续用旧 token
        return data

    def authlib_prefetched(self, data) -> str:
        """把 Yggdrasil 会话编码成 authlib-injector 需要的 prefetched 参数"""
        data = data or {}
        session = dict(data.get("session") or {})
        if not session.get("accessToken"):
            return ""
        profile = session.get("selectedProfile") or {}
        if not profile.get("id"):
            profile = {"id": (data.get("uuid") or "").replace("-", ""),
                       "name": data.get("username", "")}
            session["selectedProfile"] = profile
        session.setdefault("clientToken", data.get("client_token", ""))
        session.setdefault("availableProfiles", [profile])
        session.setdefault("user", {"id": (data.get("uuid") or "").replace("-", ""),
                                    "username": data.get("username", "")})
        raw = json.dumps(session, ensure_ascii=False).encode("utf-8")
        return base64.b64encode(raw).decode("ascii")

    def ensure_authlib_injector(self, minecraft_dir) -> str:
        """确保 authlib-injector.jar 存在且可用，返回其路径"""
        target = os.path.join(str(minecraft_dir), "authlib-injector.jar")
        if os.path.isfile(target):
            try:
                with open(target, "rb") as f:
                    if f.read(2) == b"PK":
                        return target
            except Exception:
                pass
            try:
                os.remove(target)
            except Exception:
                pass

        try:
            meta = requests.get(AUTHLIB_INJECTOR_META, timeout=20,
                                headers=_headers()).json()
            url = meta.get("download_url") or meta.get("url")
            if not url:
                raise AuthError("下载地址缺失")
            r = requests.get(url, timeout=180, headers=_headers())
            r.raise_for_status()
            if r.content[:2] != b"PK":
                raise AuthError("下载到的文件不是 jar")
            with open(target, "wb") as f:
                f.write(r.content)
            return target
        except Exception as e:  # noqa: BLE001
            raise AuthError(
                f"下载 authlib-injector 失败：{e}\n"
                "也可以手动下载 authlib-injector.jar 放到游戏目录里再启动。")

    # ------------------------------------------------------------ 通用
    def launch_info(self, minecraft_dir=None) -> dict:
        """返回启动所需的登录信息；微软 / 外置登录会在启动前自动续期"""
        auth_type = self.config.get("auth_type", "offline")
        data = dict(self.config.get("auth_data", {}) or {})

        if auth_type == "microsoft":
            data = self.refresh_microsoft()
        elif auth_type == "authlib":
            data = self.authlib_refresh()
            if not data.get("token"):
                raise AuthError("外置登录已过期，请在登录窗口重新登录")

        username = data.get("username", "")
        if not username:
            raise AuthError("还没有登录，请先点右上角「登录」")
        if auth_type == "offline":
            # 老配置里可能存了中文名，这里提前拦下来，别等进了世界才断开
            try:
                username = check_offline_name(username)
            except AuthError as e:
                raise AuthError(f"当前离线账号名「{username}」不可用：{e}")

        info = {
            "type": auth_type,
            "username": username,
            "uuid": data.get("uuid", "") or str(uuid.uuid3(uuid.NAMESPACE_URL, "offline:" + username)),
            "token": data.get("token", "0"),
        }
        if auth_type == "authlib":
            info["server"] = data.get("server", "")
            info["prefetched"] = self.authlib_prefetched(data)
        return info

    def logout(self):
        self._pending_ms = None
        self._save("offline", {"username": "", "uuid": "", "token": "0"})
