# -*- coding: utf-8 -*-
"""账户认证：离线模式 / 微软正版 OAuth / 外置登录（Authlib-Injector）

登录结果统一为 dict：
  {"type": ..., "username": ..., "uuid": ..., "token": ...}
"""
import json
import os
import uuid

import requests
import minecraft_launcher_lib.microsoft_account as ms

# 微软官方 Minecraft Launcher 的 Client ID（开源社区通用，用于 OAuth 授权码登录）
CLIENT_ID = "00000000402b5328"
REDIRECT_URI = "https://login.live.com/oauth20_desktop.srf"

AUTHLIB_INJECTOR_META = "https://authlib-injector.yushi.moe/artifact/latest.json"


class AuthError(Exception):
    pass


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
        name = (name or "").strip()
        if not name:
            raise AuthError("离线用户名不能为空")
        u = str(uuid.uuid3(uuid.NAMESPACE_URL, "offline:" + name))
        info = {"username": name, "uuid": u, "token": "0"}
        self._save("offline", info)
        return info

    # ------------------------------------------------------------ 微软正版
    def start_microsoft_login(self) -> str:
        """生成授权 URL，返回给用户浏览器打开"""
        url, state, verifier = ms.get_secure_login_data(CLIENT_ID, REDIRECT_URI)
        self._pending_ms = {"state": state, "verifier": verifier}
        return url

    def complete_microsoft_login(self, callback_url: str) -> dict:
        if not self._pending_ms:
            raise AuthError("请先发起登录")
        try:
            auth_code = ms.parse_auth_code_url(callback_url, self._pending_ms["state"])
        except Exception as e:
            raise AuthError(f"授权回调地址无效或已过期：{e}")
        verifier = self._pending_ms["verifier"]
        self._pending_ms = None
        return self._finish_microsoft(auth_code, verifier)

    def _finish_microsoft(self, auth_code, verifier=None) -> dict:
        try:
            login = ms.complete_login(CLIENT_ID, None, REDIRECT_URI, auth_code, verifier)
            return self._exchange_to_minecraft(login["access_token"], login.get("refresh_token"))
        except Exception as e:
            raise AuthError(f"微软登录失败：{e}")

    def refresh_microsoft(self) -> dict:
        """用 refresh_token 静默续期（每次启动前调用）"""
        data = self.config.get("auth_data", {})
        refresh = data.get("refresh_token")
        if not refresh:
            raise AuthError("没有可用的刷新令牌")
        try:
            login = ms.complete_refresh(CLIENT_ID, None, REDIRECT_URI, refresh)
        except Exception as e:
            raise AuthError(f"刷新令牌失败，请重新登录：{e}")
        try:
            return self._exchange_to_minecraft(login["access_token"], login.get("refresh_token") or refresh)
        except Exception as e:
            raise AuthError(f"刷新登录状态失败：{e}")

    def _exchange_to_minecraft(self, access_token, refresh_token) -> dict:
        xbl = ms.authenticate_with_xbl(access_token)
        xsts = ms.authenticate_with_xsts(xbl["Token"])
        uhs = xbl["DisplayClaims"]["xui"][0]["uhs"]
        mc = ms.authenticate_with_minecraft(uhs, xsts["Token"])
        info = {
            "username": mc["username"],
            "uuid": mc["id"],
            "token": mc["access_token"],
            "refresh_token": refresh_token or "",
        }
        self._save("microsoft", info)
        return info

    # ------------------------------------------------------------ 外置登录
    def authlib_login(self, username: str, server_url: str) -> dict:
        username = (username or "").strip()
        server_url = (server_url or "").strip().rstrip("/")
        if not username:
            raise AuthError("外置登录用户名不能为空")
        if not server_url.startswith("http"):
            raise AuthError("外置登录 API 地址需以 http(s):// 开头，例如 https://littleskin.cn/api/yggdrasil")
        # 校验服务器可访问
        try:
            r = requests.get(server_url, timeout=10)
            r.raise_for_status()
        except Exception as e:
            raise AuthError(f"无法访问外置登录服务器 {server_url}：{e}")
        info = {"username": username, "server": server_url, "uuid": "", "token": "1"}
        self._save("authlib", info)
        return info

    def ensure_authlib_injector(self, minecraft_dir) -> str:
        """确保 authlib-injector.jar 存在，返回其路径"""
        target = os.path.join(str(minecraft_dir), "authlib-injector.jar")
        if os.path.isfile(target):
            return target
        try:
            meta = requests.get(AUTHLIB_INJECTOR_META, timeout=15).json()
            url = meta.get("download_url") or meta.get("url")
            if not url:
                raise AuthError("authlib-injector 下载地址缺失")
            r = requests.get(url, timeout=120)
            r.raise_for_status()
            with open(target, "wb") as f:
                f.write(r.content)
            return target
        except Exception as e:
            raise AuthError(f"下载 authlib-injector 失败：{e}（请手动把 jar 放到游戏目录）")

    # ------------------------------------------------------------ 通用
    def launch_info(self, minecraft_dir) -> dict:
        """返回启动所需的登录信息；微软账号启动前自动续期"""
        t = self.config.get("auth_type", "offline")
        data = dict(self.config.get("auth_data", {}) or {})
        if t == "microsoft":
            try:
                data = self.refresh_microsoft()
            except AuthError:
                raise AuthError("微软账号登录已过期，请在启动器中重新登录")
        return {"type": t, "username": data.get("username", ""),
                "uuid": data.get("uuid", ""), "token": data.get("token", "0")}

    def logout(self):
        self._save("offline", {"username": "", "uuid": "", "token": "0"})
