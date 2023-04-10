import json
import logging
import re
import secrets
import sys
import urllib
from pprint import pformat
from urllib import parse

from yarl import URL

from pyhon import const

_LOGGER = logging.getLogger(__name__)


class HonAuth:
    def __init__(self, session, email, password, device) -> None:
        self._session = session
        self._email = email
        self._password = password
        self._access_token = ""
        self._refresh_token = ""
        self._cognito_token = ""
        self._id_token = ""
        self._device = device

    @property
    def cognito_token(self):
        return self._cognito_token

    @property
    def id_token(self):
        return self._id_token

    @property
    def access_token(self):
        return self._access_token

    @property
    def refresh_token(self):
        return self._refresh_token

    async def _load_login(self):
        nonce = secrets.token_hex(16)
        nonce = f"{nonce[:8]}-{nonce[8:12]}-{nonce[12:16]}-{nonce[16:20]}-{nonce[20:]}"
        params = {
            "response_type": "token+id_token",
            "client_id": const.CLIENT_ID,
            "redirect_uri": urllib.parse.quote(
                f"{const.APP}://mobilesdk/detect/oauth/done"
            ),
            "display": "touch",
            "scope": "api openid refresh_token web",
            "nonce": nonce,
        }
        params = "&".join([f"{k}={v}" for k, v in params.items()])
        async with self._session.get(
            f"{const.AUTH_API}/services/oauth2/authorize/expid_Login?{params}"
        ) as response:
            _LOGGER.debug("%s - %s", response.status, response.request_info.url)
            if not (login_url := re.findall("url = '(.+?)'", await response.text())):
                return False
        async with self._session.get(login_url[0], allow_redirects=False) as redirect1:
            _LOGGER.debug("%s - %s", redirect1.status, redirect1.request_info.url)
            if not (url := redirect1.headers.get("Location")):
                return False
        async with self._session.get(url, allow_redirects=False) as redirect2:
            _LOGGER.debug("%s - %s", redirect2.status, redirect2.request_info.url)
            if not (
                url := redirect2.headers.get("Location")
                + "&System=IoT_Mobile_App&RegistrationSubChannel=hOn"
            ):
                return False
        async with self._session.get(URL(url, encoded=True)) as login_screen:
            _LOGGER.debug("%s - %s", login_screen.status, login_screen.request_info.url)
            if context := re.findall(
                '"fwuid":"(.*?)","loaded":(\\{.*?})', await login_screen.text()
            ):
                fw_uid, loaded_str = context[0]
                loaded = json.loads(loaded_str)
                login_url = login_url[0].replace(
                    "/".join(const.AUTH_API.split("/")[:-1]), ""
                )
                return fw_uid, loaded, login_url
        return False

    async def _login(self, fw_uid, loaded, login_url):
        data = {
            "message": {
                "actions": [
                    {
                        "id": "79;a",
                        "descriptor": "apex://LightningLoginCustomController/ACTION$login",
                        "callingDescriptor": "markup://c:loginForm",
                        "params": {
                            "username": self._email,
                            "password": self._password,
                            "startUrl": parse.unquote(
                                login_url.split("startURL=")[-1]
                            ).split("%3D")[0],
                        },
                    }
                ]
            },
            "aura.context": {
                "mode": "PROD",
                "fwuid": fw_uid,
                "app": "siteforce:loginApp2",
                "loaded": loaded,
                "dn": [],
                "globals": {},
                "uad": False,
            },
            "aura.pageURI": login_url,
            "aura.token": None,
        }
        params = {"r": 3, "other.LightningLoginCustom.login": 1}
        async with self._session.post(
            const.AUTH_API + "/s/sfsites/aura",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data="&".join(f"{k}={json.dumps(v)}" for k, v in data.items()),
            params=params,
        ) as response:
            _LOGGER.debug("%s - %s", response.status, response.request_info.url)
            if response.status == 200:
                try:
                    data = await response.json()
                    return data["events"][0]["attributes"]["values"]["url"]
                except json.JSONDecodeError:
                    pass
                except KeyError:
                    _LOGGER.error(
                        "Can't get login url - %s", pformat(await response.json())
                    )
            _LOGGER.error(
                "Unable to login: %s\n%s", response.status, await response.text()
            )
            return ""

    async def _get_token(self, url):
        async with self._session.get(url) as response:
            _LOGGER.debug("%s - %s", response.status, response.request_info.url)
            if response.status != 200:
                _LOGGER.error("Unable to get token: %s", response.status)
                return False
            url = re.findall("href\\s*=\\s*[\"'](.+?)[\"']", await response.text())
        if not url:
            _LOGGER.error("Can't get login url - \n%s", await response.text())
            raise PermissionError
        if "ProgressiveLogin" in url[0]:
            async with self._session.get(url[0]) as response:
                _LOGGER.debug("%s - %s", response.status, response.request_info.url)
                if response.status != 200:
                    _LOGGER.error("Unable to get token: %s", response.status)
                    return False
                url = re.findall("href\\s*=\\s*[\"'](.*?)[\"']", await response.text())
        url = "/".join(const.AUTH_API.split("/")[:-1]) + url[0]
        async with self._session.get(url) as response:
            _LOGGER.debug("%s - %s", response.status, response.request_info.url)
            if response.status != 200:
                _LOGGER.error(
                    "Unable to connect to the login service: %s", response.status
                )
                return False
            text = await response.text()
        if access_token := re.findall("access_token=(.*?)&", text):
            self._access_token = access_token[0]
        if refresh_token := re.findall("refresh_token=(.*?)&", text):
            self._refresh_token = refresh_token[0]
        if id_token := re.findall("id_token=(.*?)&", text):
            self._id_token = id_token[0]
        return True

    async def authorize(self):
        if login_site := await self._load_login():
            fw_uid, loaded, login_url = login_site
        else:
            return False
        if not (url := await self._login(fw_uid, loaded, login_url)):
            return False
        if not await self._get_token(url):
            return False

        post_headers = {"id-token": self._id_token}
        data = self._device.get()
        async with self._session.post(
            f"{const.API_URL}/auth/v1/login", headers=post_headers, json=data
        ) as response:
            _LOGGER.debug("%s - %s", response.status, response.request_info.url)
            try:
                json_data = await response.json()
            except json.JSONDecodeError:
                _LOGGER.error("No JSON Data after POST: %s", await response.text())
                return False
            self._cognito_token = json_data["cognitoUser"]["Token"]
        return True

    async def refresh(self):
        params = {
            "client_id": const.CLIENT_ID,
            "refresh_token": self._refresh_token,
            "grant_type": "refresh_token",
        }
        async with self._session.post(
            f"{const.AUTH_API}/services/oauth2/token", params=params
        ) as response:
            _LOGGER.debug("%s - %s", response.status, response.request_info.url)
            if response.status >= 400:
                return False
            data = await response.json()
        self._id_token = data["id_token"]
        self._access_token = data["access_token"]
        return True
