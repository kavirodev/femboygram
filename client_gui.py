
import array
import asyncio
import base64
import hashlib
import json
import os
import re
import sys
import threading
import time
import uuid
from pathlib import Path

import flet as ft
import websockets
from cryptography.fernet import Fernet

SHARED_E2EE_KEY = b"X9vL2pQ7rStUvWxYzAbCdEfGhIjKlMnOpQrStUvWxYz="
cipher = Fernet(SHARED_E2EE_KEY)

SERVER_URL = "ws://100.71.221.122:8000"

MAX_ABOUT_LENGTH = 40

if os.getenv("FLET_APP_STORAGE_DATA"):
    BASE_DIR = Path(os.environ["FLET_APP_STORAGE_DATA"])
elif getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).resolve().parent
else:
    BASE_DIR = Path(__file__).resolve().parent

BASE_DIR.mkdir(parents=True, exist_ok=True)

CONFIG_FILE = str(BASE_DIR / "config.json")
LOCAL_AVATARS_DIR = BASE_DIR / "client_avatars"
PEER_AVATARS_DIR = LOCAL_AVATARS_DIR / "peers"

MAX_AVATAR_SIZE = 5 * 1024 * 1024
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
JPG_MAGIC = b"\xff\xd8\xff"


# ---------- Конфигурация ----------

def load_config():
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as file:
            value = json.load(file)

        return value if isinstance(value, dict) else None

    except (OSError, json.JSONDecodeError):
        return None


def save_config(data):
    temp = CONFIG_FILE + ".tmp"

    with open(temp, "w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=4)

    os.replace(temp, CONFIG_FILE)


def clear_config():
    try:
        os.remove(CONFIG_FILE)
    except FileNotFoundError:
        pass


# ---------- Аватарки ----------

def detect_ext(image_bytes):
    if image_bytes.startswith(PNG_MAGIC):
        return ".png"

    if image_bytes.startswith(JPG_MAGIC):
        return ".jpg"

    return None


def store_avatar(user_id, image_bytes):
    ext = detect_ext(image_bytes)

    if not ext:
        raise ValueError("Нужен PNG или JPG")

    if len(image_bytes) > MAX_AVATAR_SIZE:
        raise ValueError("Аватарка больше 5 МБ")

    LOCAL_AVATARS_DIR.mkdir(parents=True, exist_ok=True)

    digest = hashlib.sha1(image_bytes).hexdigest()[:8]

    new_path = LOCAL_AVATARS_DIR / f"{user_id}_{digest}{ext}"
    new_path.write_bytes(image_bytes)

    for old in LOCAL_AVATARS_DIR.glob(f"{user_id}*"):
        if old != new_path and old.is_file():
            old.unlink(missing_ok=True)

    return str(new_path.resolve())


def store_peer_avatar(user_id, image_bytes):
    ext = detect_ext(image_bytes)

    if not ext or len(image_bytes) > MAX_AVATAR_SIZE:
        return None

    PEER_AVATARS_DIR.mkdir(parents=True, exist_ok=True)

    digest = hashlib.sha1(image_bytes).hexdigest()[:8]

    new_path = PEER_AVATARS_DIR / f"{user_id}_{digest}{ext}"

    if not new_path.exists():
        new_path.write_bytes(image_bytes)

    for old in PEER_AVATARS_DIR.glob(f"{user_id}*"):
        if old != new_path and old.is_file():
            old.unlink(missing_ok=True)

    return str(new_path.resolve())


def avatar_image(path=None, size=42):
    if path and os.path.isfile(path):
        return ft.CircleAvatar(
            radius=size / 2,
            foreground_image_src=str(path),
            content=ft.Icon(ft.Icons.PERSON, color="white"),
            bgcolor="pink100"
        )

    return ft.CircleAvatar(
        radius=size / 2,
        content=ft.Icon(ft.Icons.PERSON, color="white"),
        bgcolor="pink400"
    )


# ---------- Голосовые звонки ----------

try:
    import miniaudio
except Exception:
    miniaudio = None

try:
    import sounddevice as sd
except Exception:
    sd = None


SAMPLE_RATE = 16000
FRAME_BYTES = SAMPLE_RATE * 20 // 1000 * 2
PREBUFFER_BYTES = FRAME_BYTES * 3
MAX_BUFFER_BYTES = FRAME_BYTES * 20


def voice_available():
    if miniaudio is None and sd is None:
        return False, "Для звонков нужна библиотека miniaudio"

    return True, ""


class VoiceEngine:

    def __init__(self, on_frame):
        self.on_frame = on_frame
        self.muted = False
        self._lock = threading.Lock()
        self._play_buf = bytearray()
        self._cap_buf = bytearray()
        self._primed = False
        self._devices = []

    def feed(self, data):
        with self._lock:
            self._play_buf += data

            extra = len(self._play_buf) - MAX_BUFFER_BYTES

            if extra > 0:
                del self._play_buf[:extra]

    def _take(self, need):
        with self._lock:
            if not self._primed:
                if len(self._play_buf) >= PREBUFFER_BYTES:
                    self._primed = True
                else:
                    return bytes(need)

            chunk = bytes(self._play_buf[:need])
            del self._play_buf[:need]

            if len(chunk) < need:
                chunk += bytes(need - len(chunk))
                self._primed = False

            return chunk

    def _on_captured(self, data):
        self._cap_buf += data

        while len(self._cap_buf) >= FRAME_BYTES:
            frame = bytes(self._cap_buf[:FRAME_BYTES])
            del self._cap_buf[:FRAME_BYTES]

            if not self.muted:
                self.on_frame(frame)

    def _playback_gen(self):
        required_frames = yield b""

        while True:
            samples = array.array("h")
            samples.frombytes(self._take(required_frames * 2))
            required_frames = yield samples

    def _capture_gen(self):
        while True:
            data = yield
            self._on_captured(bytes(data))

    def _start_miniaudio(self):
        playback = miniaudio.PlaybackDevice(
            output_format=miniaudio.SampleFormat.SIGNED16,
            nchannels=1,
            sample_rate=SAMPLE_RATE,
            buffersize_msec=40
        )

        self._devices.append(playback)

        capture = miniaudio.CaptureDevice(
            input_format=miniaudio.SampleFormat.SIGNED16,
            nchannels=1,
            sample_rate=SAMPLE_RATE,
            buffersize_msec=20
        )

        self._devices.append(capture)

        pgen = self._playback_gen()
        next(pgen)
        playback.start(pgen)

        cgen = self._capture_gen()
        next(cgen)
        capture.start(cgen)

    def _start_sounddevice(self):
        block = FRAME_BYTES // 2

        def out_cb(outdata, frames, time_info, status):
            outdata[:] = self._take(frames * 2)

        def in_cb(indata, frames, time_info, status):
            self._on_captured(bytes(indata))

        out = sd.RawOutputStream(
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype="int16",
            blocksize=block,
            callback=out_cb
        )

        self._devices.append(out)

        inp = sd.RawInputStream(
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype="int16",
            blocksize=block,
            callback=in_cb
        )

        self._devices.append(inp)

        out.start()
        inp.start()

    def start(self):
        errors = []

        for available, starter in (
            (miniaudio is not None, self._start_miniaudio),
            (sd is not None, self._start_sounddevice)
        ):
            if not available:
                continue

            try:
                starter()
                return

            except Exception as ex:
                errors.append(f"{type(ex).__name__}: {ex}")
                self.stop()

        raise RuntimeError(
            "; ".join(errors) or "Нет библиотеки для работы со звуком"
        )

    def stop(self):
        for dev in self._devices:
            try:
                dev.stop()
            except Exception:
                pass

            try:
                dev.close()
            except Exception:
                pass

        self._devices = []


# ---------- Главное приложение ----------

def main(page: ft.Page):

    page.title = "Femboygram"
    page.horizontal_alignment = ft.CrossAxisAlignment.CENTER
    page.vertical_alignment = ft.MainAxisAlignment.CENTER

    def show_auth_page(error=""):
        page.clean()

        config = load_config() or {}

        name_field = ft.TextField(label="Имя", width=310)

        username_field = ft.TextField(
            label="Юзернейм",
            width=310,
            value=config.get("username", "")
        )

        email_field = ft.TextField(
            label="Электронная почта",
            width=310,
            value=config.get("email", "")
        )

        password_field = ft.TextField(
            label="Пароль",
            password=True,
            can_reveal_password=True,
            width=310
        )

        code_field = ft.TextField(
            label="Код из письма",
            width=310,
            visible=False,
            max_length=6
        )

        remember_checkbox = ft.Checkbox(
            label="Запомнить меня",
            value=True
        )

        status = ft.Text(error, color="red", size=12)

        is_register = True
        verification_pending = False
        verification_ws = None
        registration_id = str(uuid.uuid4())

        title = ft.Text(
            "✨ Регистрация в Femboygram ✨",
            size=22,
            weight=ft.FontWeight.BOLD,
            color="pink"
        )

        async def authenticate(e):
            nonlocal is_register, verification_pending, verification_ws

            name = (name_field.value or "").strip()
            username = (username_field.value or "").lower().strip().lstrip("@")
            email = (email_field.value or "").lower().strip()
            password = password_field.value or ""
            remember = bool(remember_checkbox.value)

            if not username or not password:
                status.value = "Введи юзернейм и пароль"
                page.update()
                return

            if is_register and not verification_pending and not all((name, email)):
                status.value = "Заполни все поля для регистрации"
                page.update()
                return

            if is_register and len(password) < 6:
                status.value = "Пароль должен содержать минимум 6 символов"
                page.update()
                return

            if len(username) > 32 or re.search(r"\s", username):
                status.value = "Некорректный юзернейм"
                page.update()
                return

            submit_button.disabled = True
            switch_button.disabled = True
            status.color = "grey"
            page.update()

            try:
                if is_register and not verification_pending:
                    verification_ws = await websockets.connect(SERVER_URL)

                    await verification_ws.send(json.dumps({
                        "type": "request_verification",
                        "id": registration_id,
                        "username": username,
                        "email": email,
                        "password": password,
                        "name": name
                    }))

                    response = json.loads(
                        await asyncio.wait_for(
                            verification_ws.recv(),
                            timeout=25
                        )
                    )

                    if response.get("type") == "auth_success":
                        await verification_ws.close()
                        verification_ws = None

                        run_chat(
                            page,
                            username,
                            email,
                            password,
                            name or username,
                            on_auth_error=show_auth_page,
                            remember=remember
                        )
                        return

                    if response.get("type") != "verification_sent":
                        raise ValueError(
                            response.get("error", "Не удалось отправить код")
                        )

                    verification_pending = True
                    code_field.visible = True
                    resend_button.visible = True

                    status.value = (
                        response.get("message", "Код отправлен")
                        + ". Введи его ниже."
                    )

                    submit_button.content = "Подтвердить почту"
                    submit_button.disabled = False
                    switch_button.disabled = False

                    page.update()
                    return

                if is_register and verification_pending:
                    code = (code_field.value or "").strip().upper()

                    if len(code) != 6:
                        raise ValueError("Введи шестизначный код из письма")

                    await verification_ws.send(json.dumps({
                        "type": "verify_registration",
                        "code": code
                    }))

                    response = json.loads(
                        await asyncio.wait_for(
                            verification_ws.recv(),
                            timeout=15
                        )
                    )

                    if response.get("type") != "auth_success":
                        raise ValueError(
                            response.get("error", "Почта не подтверждена")
                        )

                    await verification_ws.close()
                    verification_ws = None
                    verification_pending = False

                    run_chat(
                        page,
                        username,
                        email,
                        password,
                        name or username,
                        on_auth_error=show_auth_page,
                        remember=remember
                    )
                    return

                run_chat(
                    page,
                    username,
                    email,
                    password,
                    username,
                    on_auth_error=show_auth_page,
                    remember=remember
                )

            except Exception as ex:
                status.value = f"Ошибка: {ex}"
                status.color = "red"

                submit_button.disabled = False
                switch_button.disabled = False

                page.update()

        async def resend_code(e=None):
            nonlocal verification_ws

            if not verification_ws:
                return

            try:
                await verification_ws.send(json.dumps({
                    "type": "resend_verification",
                    "id": registration_id,
                    "username": (username_field.value or "").lower().strip().lstrip("@"),
                    "email": (email_field.value or "").lower().strip(),
                    "password": password_field.value or "",
                    "name": (name_field.value or "").strip()
                }))

                response = json.loads(
                    await asyncio.wait_for(verification_ws.recv(), timeout=25)
                )

                status.value = response.get(
                    "message",
                    response.get("error", "Ответ сервера")
                )

                status.color = (
                    "red"
                    if response.get("type") == "verification_error"
                    else "green"
                )

            except Exception as ex:
                status.value = str(ex)
                status.color = "red"

            page.update()

        def switch_mode(e=None):
            nonlocal is_register, verification_pending, verification_ws

            if verification_ws:
                page.run_task(verification_ws.close)
                verification_ws = None

            verification_pending = False
            is_register = not is_register

            name_field.visible = is_register
            email_field.visible = is_register
            code_field.visible = False
            resend_button.visible = False

            title.value = (
                "✨ Регистрация в Femboygram ✨"
                if is_register
                else "✨ Вход в Femboygram ✨"
            )

            submit_button.content = (
                "Зарегистрироваться" if is_register else "Войти"
            )

            switch_button.content = (
                "Уже есть аккаунт? Войти"
                if is_register
                else "Нет аккаунта? Зарегистрироваться"
            )

            status.value = ""
            page.update()

        submit_button = ft.Button(
            content="Зарегистрироваться",
            width=310,
            height=45,
            bgcolor="pink400",
            color="white",
            on_click=authenticate
        )

        switch_button = ft.TextButton(
            content="Уже есть аккаунт? Войти",
            on_click=switch_mode
        )

        resend_button = ft.TextButton(
            content="Отправить код повторно",
            on_click=resend_code,
            visible=False
        )

        page.add(ft.Column(
            controls=[
                title,
                name_field,
                username_field,
                email_field,
                password_field,
                code_field,
                ft.Container(
                    content=remember_checkbox,
                    width=310,
                    alignment=ft.Alignment.CENTER_LEFT
                ),
                status,
                submit_button,
                resend_button,
                switch_button
            ],
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            spacing=12
        ))

        page.update()

    config = load_config() or {}

    if all(config.get(k) for k in ("username", "password")):
        run_chat(
            page,
            config["username"],
            config.get("email", ""),
            config["password"],
            config.get("name") or config["username"],
            on_auth_error=show_auth_page,
            remember=True
        )
    else:
        show_auth_page()


# ---------- Чат ----------

def run_chat(
    page,
    username,
    email,
    password,
    name,
    is_register=False,
    on_auth_error=None,
    remember=True
):
    page.clean()

    page.title = "Femboygram"
    page.horizontal_alignment = ft.CrossAxisAlignment.CENTER
    page.vertical_alignment = ft.MainAxisAlignment.CENTER

    page.add(ft.Column(
        [
            ft.ProgressRing(color="pink"),
            ft.Text("Проверяем аккаунт...", color="pink")
        ],
        horizontal_alignment=ft.CrossAxisAlignment.CENTER
    ))

    config = load_config() or {}

    my_id = config.get("id") or str(uuid.uuid4())
    about = config.get("about", "")

    websocket = None
    target_id = None

    pending_profile_update = None
    auth_future = None

    is_authenticated = False
    interface_ready = False

    online_ids = set()

    current_avatar_path = config.get("avatar_path")

    profiles = {}
    requested = set()
    contacts = {}
    search_order = []
    chat_messages = []

    call = {
        "state": "idle",
        "id": None,
        "peer": None,
        "engine": None,
        "tasks": [],
        "queue": None,
        "cancel": False
    }

    call_status = ft.Text("", size=12, color="grey")

    call_panel = ft.Container(
        visible=False,
        padding=8,
        border_radius=10,
        bgcolor="black26"
    )

    header_box = ft.Container(
        content=ft.Text(
            "Выберите чат",
            size=17,
            weight=ft.FontWeight.BOLD
        )
    )

    chat_list = ft.ListView(
        expand=True,
        spacing=10,
        padding=10,
        auto_scroll=True
    )

    message_input = ft.TextField(
        hint_text="Введите сообщение...",
        expand=True,
        border_color="pink300"
    )

    users_column = ft.ListView(
        expand=True,
        spacing=5,
        padding=4
    )

    search_input = ft.TextField(
        hint_text="🔍 Поиск юзернейма...",
        expand=True,
        height=44,
        text_size=13,
        prefix_icon=ft.Icons.SEARCH
    )

    self_avatar = avatar_image(current_avatar_path, 46)

    self_name_text = ft.Text(
        name,
        weight=ft.FontWeight.BOLD,
        size=13
    )

    self_username_text = ft.Text(
        f"@{username}",
        size=11,
        color="grey"
    )

    # ---------- Вспомогательные функции ----------

    def send_async(payload):
        async def _send():
            if websocket and is_authenticated:
                try:
                    await websocket.send(json.dumps(payload))
                except Exception as ex:
                    print(f"[SEND ERROR] {ex}")

        page.run_task(_send)

    def display_name(uid):
        nick = contacts.get(uid)

        if nick:
            return nick

        profile = profiles.get(uid, {})

        return (
            profile.get("name")
            or profile.get("username")
            or "Пользователь"
        )

    def peer_avatar(uid, size):
        return avatar_image(
            profiles.get(uid, {}).get("avatar_path"),
            size
        )

    def ensure_profile(
        uid,
        uname=None,
        real_name=None,
        version=None,
        user_about=None
    ):
        profile = profiles.setdefault(uid, {})

        if uname is not None:
            profile["username"] = uname

        if real_name is not None:
            profile["name"] = real_name

        if user_about is not None:
            profile["about"] = user_about

        if version is None:
            need = "version" not in profile
        else:
            need = profile.get("version") != version

        if need and uid not in requested:
            requested.add(uid)
            send_async({
                "type": "get_profile",
                "user_id": uid
            })

    def refresh_own_avatar(path=None):
        nonlocal current_avatar_path

        if path is not None:
            current_avatar_path = path

        if current_avatar_path and os.path.isfile(current_avatar_path):
            self_avatar.foreground_image_src = str(current_avatar_path)
            self_avatar.bgcolor = "pink100"
        else:
            self_avatar.foreground_image_src = None
            self_avatar.bgcolor = "pink400"
            self_avatar.content = ft.Icon(
                ft.Icons.PERSON,
                color="white",
                size=25
            )

        self_avatar.update()

    # ---------- ПРОФИЛЬ СОБЕСЕДНИКА ----------

    def open_user_profile(uid):
        profile = profiles.get(uid, {})

        avatar = avatar_image(
            profile.get("avatar_path"),
            100
        )

        profile_name = ft.Text(
            profile.get("name") or "Пользователь",
            size=22,
            weight=ft.FontWeight.BOLD,
            text_align=ft.TextAlign.CENTER
        )

        profile_username = ft.Text(
            f"@{profile.get('username') or 'unknown'}",
            size=14,
            color="pink300",
            text_align=ft.TextAlign.CENTER
        )

        about_text = ft.Text(
            profile.get("about") or "Пользователь пока ничего не рассказал о себе.",
            size=14,
            text_align=ft.TextAlign.CENTER,
            color="grey" if not profile.get("about") else None
        )

        about_title = ft.Text(
            "ОБО МНЕ",
            size=12,
            weight=ft.FontWeight.BOLD,
            color="pink"
        )

        dialog = ft.AlertDialog(
            modal=True,
            title=ft.Text(
                "Профиль пользователя",
                text_align=ft.TextAlign.CENTER
            ),
            content=ft.Container(
                width=340,
                content=ft.Column(
                    [
                        ft.Container(
                            content=avatar,
                            alignment=ft.Alignment.CENTER
                        ),
                        profile_name,
                        profile_username,
                        ft.Divider(),
                        about_title,
                        ft.Container(
                            content=about_text,
                            padding=12,
                            border_radius=10,
                            bgcolor="black12",
                            width=320
                        )
                    ],
                    tight=True,
                    horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                    spacing=10
                )
            ),
            actions=[
                ft.TextButton(
                    content="Закрыть",
                    on_click=lambda e: page.pop_dialog()
                )
            ],
            actions_alignment=ft.MainAxisAlignment.CENTER
        )

        page.show_dialog(dialog)

    def open_current_profile(e=None):
        if target_id:
            ensure_profile(target_id)
            open_user_profile(target_id)

    # ---------- Сообщения ----------

    def make_bubble(msg):
        deleted = bool(msg.get("deleted"))

        text = (
            "Сообщение удалено"
            if deleted
            else msg["text"]
        )

        if msg["mine"]:
            return ft.Container(
                content=ft.Text(
                    text,
                    color="white",
                    italic=deleted
                ),
                bgcolor="pink600",
                border_radius=10,
                padding=9,
                alignment=ft.Alignment.CENTER_RIGHT
            )

        sender = msg["sender"]

        return ft.Row(
            [
                peer_avatar(sender, 34),
                ft.Column(
                    [
                        ft.Text(
                            display_name(sender),
                            size=11,
                            weight=ft.FontWeight.BOLD,
                            color="pink200"
                        ),
                        ft.Container(
                            content=ft.Text(
                                text,
                                italic=deleted
                            ),
                            bgcolor="blueGrey800",
                            border_radius=10,
                            padding=9
                        )
                    ],
                    spacing=2,
                    expand=True
                )
            ],
            vertical_alignment=ft.CrossAxisAlignment.START,
            spacing=8
        )

    def render_chat():
        chat_list.controls.clear()

        for message in chat_messages:
            chat_list.controls.append(make_bubble(message))

    # ---------- Контакты ----------

    def open_rename_dialog(uid):
        profile = profiles.get(uid, {})

        real = (
            profile.get("name")
            or profile.get("username")
            or ""
        )

        field = ft.TextField(
            label="Имя в ваших контактах",
            value=contacts.get(uid, ""),
            hint_text=real,
            width=330,
            max_length=64
        )

        def save(ev):
            send_async({
                "type": "rename_contact",
                "contact_id": uid,
                "nickname": (field.value or "").strip()
            })

            page.pop_dialog()

        dialog = ft.AlertDialog(
            modal=True,
            title=ft.Text("Изменить имя контакта"),
            content=ft.Column(
                [
                    ft.Text(
                        f"Настоящее имя: {real} "
                        f"(@{profile.get('username', '')})",
                        size=12,
                        color="grey"
                    ),
                    ft.Text(
                        "Новое имя увидите только вы. "
                        "Пустое поле вернёт настоящее имя.",
                        size=12,
                        color="grey"
                    ),
                    field
                ],
                tight=True,
                spacing=10,
                width=360
            ),
            actions=[
                ft.TextButton(
                    content="Отмена",
                    on_click=lambda ev: page.pop_dialog()
                ),
                ft.Button(
                    content="Сохранить",
                    bgcolor="pink400",
                    color="white",
                    on_click=save
                )
            ]
        )

        page.show_dialog(dialog)

    def update_header():
        if not target_id:
            header_box.content = ft.Text(
                "Выберите чат",
                size=17,
                weight=ft.FontWeight.BOLD
            )

        else:
            uid = target_id
            profile = profiles.get(uid, {})

            if uid in contacts:
                actions = [
                    ft.IconButton(
                        icon=ft.Icons.EDIT,
                        icon_color="pink",
                        tooltip="Изменить имя",
                        on_click=lambda ev, c=uid: open_rename_dialog(c)
                    ),
                    ft.IconButton(
                        icon=ft.Icons.PERSON_REMOVE,
                        icon_color="pink",
                        tooltip="Удалить из контактов",
                        on_click=lambda ev, c=uid: send_async({
                            "type": "remove_contact",
                            "contact_id": c
                        })
                    )
                ]

            else:
                actions = [
                    ft.IconButton(
                        icon=ft.Icons.PERSON_ADD,
                        icon_color="pink",
                        tooltip="Добавить в контакты",
                        on_click=lambda ev, c=uid: send_async({
                            "type": "add_contact",
                            "contact_id": c
                        })
                    )
                ]

            actions.insert(
                0,
                ft.IconButton(
                    icon=ft.Icons.CALL,
                    icon_color="green",
                    tooltip="Позвонить",
                    on_click=lambda ev: start_call()
                )
            )

            # Имя и аватарка кликабельны.
            profile_button = ft.Container(
                content=ft.Row(
                    [
                        peer_avatar(uid, 40),
                        ft.Column(
                            [
                                ft.Text(
                                    display_name(uid),
                                    size=17,
                                    weight=ft.FontWeight.BOLD
                                ),
                                ft.Text(
                                    f"@{profile.get('username', '')}",
                                    size=11,
                                    color="grey"
                                )
                            ],
                            spacing=0
                        )
                    ],
                    spacing=10
                ),
                ink=True,
                border_radius=10,
                padding=4,
                on_click=lambda ev: open_current_profile()
            )

            header_box.content = ft.Row(
                [
                    profile_button,
                    ft.Container(expand=True),
                    *actions
                ],
                spacing=10
            )

        if interface_ready:
            header_box.update()

    def select_chat(uid):
        nonlocal target_id

        if not is_authenticated:
            return

        target_id = uid

        ensure_profile(uid)

        chat_messages.clear()
        chat_list.controls.clear()

        chat_list.controls.append(
            ft.Text("Загрузка истории...", color="grey")
        )

        update_header()
        page.update()

        send_async({
            "type": "get_chat_history",
            "target_id": uid
        })

    def make_user_row(uid, show_add):
        profile = profiles.get(uid, {})

        trailing = [
            ft.Icon(
                ft.Icons.CIRCLE,
                size=9,
                color="green" if uid in online_ids else "grey"
            )
        ]

        if show_add and uid not in contacts:
            trailing.insert(
                0,
                ft.IconButton(
                    icon=ft.Icons.PERSON_ADD,
                    icon_size=18,
                    icon_color="pink",
                    tooltip="Добавить в контакты",
                    on_click=lambda ev, c=uid: send_async({
                        "type": "add_contact",
                        "contact_id": c
                    })
                )
            )

        return ft.Container(
            content=ft.Row(
                [
                    peer_avatar(uid, 36),
                    ft.Column(
                        [
                            ft.Text(
                                display_name(uid),
                                weight=ft.FontWeight.BOLD,
                                size=13
                            ),
                            ft.Text(
                                f"@{profile.get('username', 'unknown')}",
                                size=10,
                                color="grey"
                            )
                        ],
                        spacing=0,
                        expand=True
                    ),
                    *trailing
                ],
                spacing=8
            ),
            padding=7,
            border_radius=9,
            ink=True,
            on_click=lambda ev, c=uid: select_chat(c)
        )

    def render_sidebar():
        users_column.controls.clear()

        query = (search_input.value or "").strip()

        if query:
            users_column.controls.append(
                ft.Text("Результаты поиска", size=12, color="grey")
            )

            if search_order:
                for uid in search_order:
                    users_column.controls.append(
                        make_user_row(uid, show_add=True)
                    )
            else:
                users_column.controls.append(
                    ft.Text("Никого не найдено", size=12, color="grey")
                )

        else:
            users_column.controls.append(
                ft.Text("Контакты", size=12, color="grey")
            )

            if contacts:
                for uid in sorted(
                    contacts,
                    key=lambda c: display_name(c).lower()
                ):
                    users_column.controls.append(
                        make_user_row(uid, show_add=False)
                    )
            else:
                users_column.controls.append(
                    ft.Text(
                        "Контактов пока нет. "
                        "Найди человека через поиск и добавь его.",
                        size=12,
                        color="grey"
                    )
                )

        if interface_ready:
            users_column.update()

    def refresh_ui():
        render_sidebar()
        update_header()

        if target_id:
            render_chat()

        if interface_ready:
            page.update()

    # ---------- Звонки ----------

    def show_snack(text):
        page.show_dialog(
            ft.SnackBar(content=ft.Text(text))
        )

    def render_call_panel():
        state = call["state"]

        if state == "idle":
            call_panel.visible = False

        else:
            peer = call["peer"]

            if state == "outgoing":
                call_status.value = "Звоним…"

                buttons = [
                    ft.IconButton(
                        icon=ft.Icons.CALL_END,
                        icon_color="red",
                        tooltip="Отменить",
                        on_click=lambda ev: hangup_call()
                    )
                ]

            elif state == "incoming":
                call_status.value = "Входящий звонок"

                buttons = [
                    ft.IconButton(
                        icon=ft.Icons.CALL,
                        icon_color="green",
                        tooltip="Принять",
                        on_click=lambda ev: accept_call()
                    ),
                    ft.IconButton(
                        icon=ft.Icons.CALL_END,
                        icon_color="red",
                        tooltip="Отклонить",
                        on_click=lambda ev: decline_call()
                    )
                ]

            else:
                engine = call["engine"]
                muted = bool(engine and engine.muted)

                buttons = [
                    ft.IconButton(
                        icon=(
                            ft.Icons.MIC_OFF
                            if muted
                            else ft.Icons.MIC
                        ),
                        icon_color="orange" if muted else "white",
                        tooltip=(
                            "Включить микрофон"
                            if muted
                            else "Выключить микрофон"
                        ),
                        on_click=lambda ev: toggle_mute()
                    ),
                    ft.IconButton(
                        icon=ft.Icons.CALL_END,
                        icon_color="red",
                        tooltip="Завершить",
                        on_click=lambda ev: hangup_call()
                    )
                ]

            call_panel.content = ft.Row(
                [
                    peer_avatar(peer, 36),
                    ft.Column(
                        [
                            ft.Text(
                                display_name(peer),
                                weight=ft.FontWeight.BOLD,
                                size=14
                            ),
                            call_status
                        ],
                        spacing=0,
                        expand=True
                    ),
                    *buttons
                ],
                spacing=10
            )

            call_panel.visible = True

        if interface_ready:
            call_panel.update()

    def finish_call(text=""):
        for task in call["tasks"]:
            task.cancel()

        call["tasks"] = []

        engine = call["engine"]

        if engine:
            engine.stop()

        was_busy = call["state"] != "idle"

        call.update(
            state="idle",
            id=None,
            peer=None,
            engine=None,
            queue=None,
            cancel=False
        )

        render_call_panel()

        if text and was_busy and interface_ready:
            show_snack(text)
            page.update()

    def start_call():
        if (
            call["state"] != "idle"
            or not target_id
            or not is_authenticated
        ):
            return

        ok, msg = voice_available()

        if not ok:
            show_snack(msg)
            page.update()
            return

        call.update(
            state="outgoing",
            id=None,
            peer=target_id,
            cancel=False
        )

        ensure_profile(target_id)

        render_call_panel()
        page.update()

        send_async({
            "type": "call_invite",
            "target_id": target_id
        })

    def accept_call():
        if call["state"] != "incoming" or not call["id"]:
            return

        ok, msg = voice_available()

        if not ok:
            send_async({
                "type": "call_decline",
                "call_id": call["id"]
            })

            finish_call(msg)
            return

        send_async({
            "type": "call_accept",
            "call_id": call["id"]
        })

    def decline_call():
        if call["id"]:
            send_async({
                "type": "call_decline",
                "call_id": call["id"]
            })

        finish_call("")

    def hangup_call():
        if call["id"]:
            send_async({
                "type": "call_end",
                "call_id": call["id"]
            })

        elif call["state"] == "outgoing":
            call["cancel"] = True
            return

        finish_call("")

    def toggle_mute():
        engine = call["engine"]

        if engine:
            engine.muted = not engine.muted
            render_call_panel()
            page.update()

    async def begin_voice():
        loop = asyncio.get_running_loop()
        queue = asyncio.Queue(maxsize=50)

        call["queue"] = queue

        def enqueue(frame):
            try:
                queue.put_nowait(frame)
            except asyncio.QueueFull:
                pass

        engine = VoiceEngine(
            lambda frame: loop.call_soon_threadsafe(enqueue, frame)
        )

        engine.start()
        call["engine"] = engine

        async def sender():
            while True:
                frame = await queue.get()

                if websocket and is_authenticated:
                    await websocket.send(json.dumps({
                        "type": "call_audio",
                        "data": base64.b64encode(frame).decode()
                    }))

        async def timer():
            started = time.monotonic()

            while True:
                await asyncio.sleep(1)

                seconds = int(time.monotonic() - started)

                call_status.value = (
                    f"Разговор {seconds // 60:02d}:{seconds % 60:02d}"
                )

                if interface_ready:
                    call_status.update()

        call["tasks"] = [
            asyncio.create_task(sender()),
            asyncio.create_task(timer())
        ]

    END_REASONS = {
        "offline": "Пользователь не в сети",
        "busy": "Абонент занят",
        "declined": "Звонок отклонён",
        "cancelled": "Звонок отменён",
        "no_answer": "Нет ответа",
        "ended": "Звонок завершён",
        "disconnected": "Связь потеряна"
    }

    # ---------- Отправка сообщений ----------

    async def send_message(e=None):
        message = (message_input.value or "").strip()

        if not message or not target_id or not websocket or not is_authenticated:
            return

        try:
            encrypted = cipher.encrypt(message.encode()).decode()

            await websocket.send(json.dumps({
                "type": "private_msg",
                "recipient_id": target_id,
                "content": encrypted
            }))

            msg = {
                "mine": True,
                "sender": my_id,
                "text": message,
                "deleted": False
            }

            chat_messages.append(msg)
            chat_list.controls.append(make_bubble(msg))

            message_input.value = ""
            page.update()

        except Exception as ex:
            page.show_dialog(
                ft.SnackBar(content=ft.Text(f"Ошибка отправки: {ex}"))
            )

    # ---------- Приём данных сервера ----------

    async def listen_server():
        nonlocal pending_profile_update
        nonlocal auth_future
        nonlocal is_authenticated
        nonlocal about

        try:
            async for raw in websocket:
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                kind = data.get("type")

                if kind == "auth_success":
                    is_authenticated = True

                    if auth_future and not auth_future.done():
                        auth_future.set_result(data)

                elif kind == "auth_error":
                    is_authenticated = False

                    if auth_future and not auth_future.done():
                        auth_future.set_result(data)

                elif not is_authenticated:
                    continue

                elif kind == "user_list":
                    online_ids.clear()
                    online_ids.update(data.get("online_ids", []))
                    render_sidebar()

                elif kind == "search_result":
                    search_order.clear()

                    for result in data.get("results", []):
                        uid = result.get("id")

                        if not uid or uid == my_id:
                            continue

                        search_order.append(uid)

                        ensure_profile(
                            uid,
                            result.get("username"),
                            result.get("name"),
                            result.get("avatar_version", ""),
                            result.get("about", "")
                        )

                    render_sidebar()

                    if interface_ready:
                        page.update()

                elif kind == "contacts_list":
                    contacts.clear()

                    for contact in data.get("contacts", []):
                        uid = contact["id"]

                        contacts[uid] = contact.get("nickname") or ""

                        ensure_profile(
                            uid,
                            contact.get("username"),
                            contact.get("name"),
                            contact.get("avatar_version", ""),
                            contact.get("about", "")
                        )

                    refresh_ui()

                elif kind == "profile":
                    uid = data.get("user_id")

                    requested.discard(uid)

                    profile = profiles.setdefault(uid, {})

                    profile["username"] = data.get("username")
                    profile["name"] = data.get("name")
                    profile["about"] = data.get("about", "")
                    profile["version"] = data.get("avatar_version", "")

                    avatar_data = data.get("avatar_data")
                    profile["avatar_path"] = None

                    if avatar_data:
                        try:
                            raw_img = base64.b64decode(
                                avatar_data,
                                validate=True
                            )

                            profile["avatar_path"] = await asyncio.to_thread(
                                store_peer_avatar,
                                uid,
                                raw_img
                            )

                        except Exception as ex:
                            print(f"[AVATAR] {ex}")

                    refresh_ui()

                elif kind == "profile_changed":
                    uid = data.get("user_id")

                    if uid in profiles and uid != my_id:
                        profiles[uid].pop("version", None)
                        requested.discard(uid)
                        ensure_profile(uid)

                elif kind == "private_msg":
                    sender = data.get("sender_id")

                    ensure_profile(sender)

                    if sender == target_id:
                        try:
                            decrypted = cipher.decrypt(
                                data["content"].encode()
                            ).decode()
                        except Exception:
                            decrypted = "[Ошибка расшифровки]"

                        msg = {
                            "mine": False,
                            "sender": sender,
                            "text": decrypted,
                            "deleted": False
                        }

                        chat_messages.append(msg)
                        chat_list.controls.append(make_bubble(msg))
                        page.update()

                elif kind == "chat_history":
                    chat_messages.clear()

                    for item in data.get("history", []):
                        deleted = bool(item.get("is_deleted"))
                        message = ""

                        if not deleted:
                            try:
                                message = cipher.decrypt(
                                    item["content"].encode()
                                ).decode()
                            except Exception:
                                message = "[Ошибка расшифровки]"

                        sender = item.get("sender_id")

                        chat_messages.append({
                            "mine": sender == my_id,
                            "sender": sender,
                            "text": message,
                            "deleted": deleted
                        })

                    render_chat()
                    page.update()

                elif kind == "call_incoming":
                    if call["state"] != "idle":
                        send_async({
                            "type": "call_decline",
                            "call_id": data.get("call_id")
                        })

                    else:
                        call.update(
                            state="incoming",
                            id=data.get("call_id"),
                            peer=data.get("from_id"),
                            cancel=False
                        )

                        ensure_profile(call["peer"])
                        render_call_panel()
                        page.update()

                elif kind == "call_ringing":
                    if call["state"] == "outgoing":
                        call["id"] = data.get("call_id")

                        if call["cancel"]:
                            send_async({
                                "type": "call_end",
                                "call_id": call["id"]
                            })

                            finish_call("")

                elif kind == "call_started":
                    if (
                        call["state"] in ("outgoing", "incoming")
                        and data.get("call_id") == call["id"]
                    ):
                        call["state"] = "active"
                        call_status.value = "Разговор 00:00"

                        try:
                            await begin_voice()
                            render_call_panel()
                            page.update()

                        except Exception as ex:
                            print(f"[CALL] Не удалось запустить звук: {ex}")

                            send_async({
                                "type": "call_end",
                                "call_id": call["id"]
                            })

                            finish_call(f"Не удалось включить звук: {ex}")

                elif kind == "call_audio":
                    engine = call["engine"]

                    if engine and call["state"] == "active":
                        try:
                            engine.feed(
                                base64.b64decode(data.get("data", ""))
                            )
                        except Exception:
                            pass

                elif kind == "call_ended":
                    cid = data.get("call_id")

                    same_call = (
                        cid is None
                        or call["id"] is None
                        or cid == call["id"]
                    )

                    if call["state"] != "idle" and same_call:
                        finish_call(
                            END_REASONS.get(
                                data.get("reason"),
                                "Звонок завершён"
                            )
                        )

                elif kind in ("profile_updated", "profile_update_error"):
                    if pending_profile_update and not pending_profile_update.done():
                        pending_profile_update.set_result(data)

                elif kind == "server_error":
                    page.show_dialog(
                        ft.SnackBar(
                            content=ft.Text(
                                data.get("error", "Ошибка сервера")
                            )
                        )
                    )

        except websockets.ConnectionClosed as ex:
            print(f"[WEBSOCKET] Соединение закрыто: {ex}")

            if auth_future and not auth_future.done():
                auth_future.set_result({
                    "type": "auth_error",
                    "error": "Сервер закрыл соединение"
                })

        except Exception as ex:
            print(f"[LISTENER ERROR] {type(ex).__name__}: {ex}")

            if auth_future and not auth_future.done():
                auth_future.set_result({
                    "type": "auth_error",
                    "error": str(ex)
                })

    # ---------- Выход ----------

    async def logout(e=None):
        nonlocal is_authenticated, websocket

        if call["state"] != "idle":
            if call["id"]:
                send_async({
                    "type": "call_end",
                    "call_id": call["id"]
                })

            finish_call("")

        is_authenticated = False

        try:
            if websocket and websocket.state.name == "OPEN":
                await websocket.send(json.dumps({"type": "logout"}))
                await websocket.close()

        except Exception as ex:
            print(f"[LOGOUT] {ex}")

        clear_config()

        if on_auth_error:
            on_auth_error("")

    # ---------- Подключение ----------

    async def init_ws():
        nonlocal websocket
        nonlocal auth_future
        nonlocal is_authenticated
        nonlocal my_id
        nonlocal current_avatar_path
        nonlocal name
        nonlocal username
        nonlocal email
        nonlocal about

        listener_task = None

        try:
            websocket = await websockets.connect(SERVER_URL)

            auth_future = asyncio.get_running_loop().create_future()

            listener_task = asyncio.create_task(listen_server())

            await websocket.send(json.dumps({
                "type": "login",
                "id": my_id,
                "username": username,
                "email": email,
                "password": password,
                "name": name
            }))

            result = await asyncio.wait_for(
                auth_future,
                timeout=15
            )

            if result.get("type") != "auth_success":
                if websocket.state.name == "OPEN":
                    await websocket.close()

                if on_auth_error:
                    on_auth_error(
                        result.get("error", "Не удалось войти")
                    )

                return

            my_id = result.get("id", my_id)
            username = result.get("username") or username
            email = result.get("email") or email
            name = result.get("name") or name
            about = result.get("about", "")

            self_name_text.value = name
            self_username_text.value = f"@{username}"

            avatar_data = result.get("avatar_data")

            if avatar_data:
                image_bytes = base64.b64decode(
                    avatar_data,
                    validate=True
                )

                current_avatar_path = await asyncio.to_thread(
                    store_avatar,
                    my_id,
                    image_bytes
                )
            else:
                current_avatar_path = None

            if remember:
                cfg = load_config() or {}

                cfg.update({
                    "id": my_id,
                    "username": username,
                    "email": email,
                    "password": password,
                    "name": name,
                    "about": about
                })

                if current_avatar_path:
                    cfg["avatar_path"] = current_avatar_path
                else:
                    cfg.pop("avatar_path", None)

                save_config(cfg)

            else:
                clear_config()

            page.clean()

            build_chat_interface()
            refresh_own_avatar(current_avatar_path)

            await websocket.send(json.dumps({
                "type": "get_contacts"
            }))

            await listener_task

        except asyncio.TimeoutError:
            if on_auth_error:
                on_auth_error(
                    "Сервер не подтвердил авторизацию за 15 секунд"
                )

        except Exception as ex:
            print(f"[CONNECTION ERROR] {type(ex).__name__}: {ex}")

            if on_auth_error:
                on_auth_error(f"Ошибка подключения: {ex}")

        finally:
            if listener_task and not listener_task.done():
                listener_task.cancel()

    # ---------- Настройки профиля ----------

    file_picker = ft.FilePicker()

    async def open_settings(e=None):
        nonlocal pending_profile_update
        nonlocal name
        nonlocal username
        nonlocal current_avatar_path
        nonlocal about

        if not websocket or not is_authenticated:
            return

        selected_avatar_bytes = None

        name_field = ft.TextField(
            label="Отображаемое имя",
            value=name,
            width=330
        )

        username_field = ft.TextField(
            label="Юзернейм",
            value=username,
            width=330
        )

        # Новое поле «Обо мне».
        about_field = ft.TextField(
            label="Обо мне",
            hint_text="Расскажи немного о себе...",
            value=about,
            width=330,
            max_length=MAX_ABOUT_LENGTH,
            multiline=True,
            min_lines=2,
            max_lines=2
        )

        about_counter = ft.Text(
            f"{len(about)}/40",
            size=11,
            color="grey"
        )

        def update_about_counter(e):
            value = about_field.value or ""
            about_counter.value = f"{len(value)}/40"
            about_counter.update()

        about_field.on_change = update_about_counter

        avatar_preview = avatar_image(
            current_avatar_path,
            100
        )

        avatar_filename = ft.Text(
            "Текущая аватарка",
            size=12,
            color="grey"
        )

        status_text = ft.Text(
            "",
            size=12,
            color="red"
        )

        async def choose_avatar(ev):
            nonlocal selected_avatar_bytes

            try:
                files = await file_picker.pick_files(
                    allow_multiple=False,
                    file_type=ft.FilePickerFileType.CUSTOM,
                    allowed_extensions=["png", "jpg", "jpeg"],
                    with_data=True
                )

                if not files:
                    return

                picked = files[0]

                raw = picked.bytes or (
                    await asyncio.to_thread(
                        lambda: Path(picked.path).read_bytes()
                    )
                    if picked.path
                    else None
                )

                if not raw:
                    raise ValueError("Не удалось прочитать изображение")

                if len(raw) > MAX_AVATAR_SIZE:
                    raise ValueError("Аватарка больше 5 МБ")

                if not detect_ext(raw):
                    raise ValueError("Нужен настоящий PNG или JPG")

                selected_avatar_bytes = raw

                ext = "png" if raw.startswith(PNG_MAGIC) else "jpeg"

                avatar_preview.foreground_image_src = (
                    f"data:image/{ext};base64,"
                    f"{base64.b64encode(raw).decode()}"
                )

                avatar_filename.value = f"Выбрано: {picked.name}"

                page.update()

            except Exception as ex:
                status_text.value = str(ex)
                page.update()

        async def save_settings(ev):
            nonlocal name
            nonlocal username
            nonlocal current_avatar_path
            nonlocal pending_profile_update
            nonlocal about

            new_name = (name_field.value or "").strip()

            new_username = (
                (username_field.value or "")
                .lower()
                .strip()
                .lstrip("@")
            )

            new_about = (about_field.value or "").strip()

            if not new_name or not new_username:
                status_text.value = "Проверь имя и юзернейм"
                page.update()
                return

            if len(new_username) > 32 or re.search(r"\s", new_username):
                status_text.value = "Некорректный юзернейм"
                page.update()
                return

            if len(new_about) > MAX_ABOUT_LENGTH:
                status_text.value = "Максимум 40 символов"
                page.update()
                return

            try:
                pending_profile_update = (
                    asyncio.get_running_loop().create_future()
                )

                avatar_b64 = (
                    base64.b64encode(selected_avatar_bytes).decode()
                    if selected_avatar_bytes
                    else None
                )

                await websocket.send(json.dumps({
                    "type": "update_profile",
                    "name": new_name,
                    "username": new_username,
                    "about": new_about,
                    "avatar_bytes": avatar_b64
                }))

                result = await asyncio.wait_for(
                    pending_profile_update,
                    timeout=15
                )

                if result.get("type") == "profile_update_error":
                    raise ValueError(
                        result.get("error", "Ошибка профиля")
                    )

                name = new_name
                username = new_username
                about = new_about

                if selected_avatar_bytes:
                    current_avatar_path = await asyncio.to_thread(
                        store_avatar,
                        my_id,
                        selected_avatar_bytes
                    )

                if remember:
                    cfg = load_config() or {}

                    cfg.update({
                        "id": my_id,
                        "name": name,
                        "username": username,
                        "email": email,
                        "password": password,
                        "about": about
                    })

                    if current_avatar_path:
                        cfg["avatar_path"] = current_avatar_path

                    save_config(cfg)

                self_name_text.value = name
                self_username_text.value = f"@{username}"

                refresh_own_avatar(current_avatar_path)

                page.pop_dialog()

                page.show_dialog(
                    ft.SnackBar(
                        content=ft.Text("Профиль успешно обновлён!")
                    )
                )

            except Exception as ex:
                status_text.value = str(ex)

            finally:
                pending_profile_update = None
                page.update()

        dialog = ft.AlertDialog(
            modal=True,
            title=ft.Text("⚙️ Настройки профиля"),
            content=ft.Column(
                [
                    ft.Container(
                        content=avatar_preview,
                        alignment=ft.Alignment.CENTER
                    ),
                    avatar_filename,
                    ft.Button(
                        content="Выбрать аватарку",
                        on_click=choose_avatar
                    ),
                    ft.Divider(),
                    name_field,
                    username_field,
                    ft.Divider(),
                    ft.Text(
                        "Обо мне",
                        size=14,
                        weight=ft.FontWeight.BOLD,
                        color="pink"
                    ),
                    about_field,
                    ft.Container(
                        content=about_counter,
                        alignment=ft.Alignment.CENTER_RIGHT,
                        width=330
                    ),
                    status_text
                ],
                tight=True,
                spacing=10,
                width=360,
                scroll=ft.ScrollMode.AUTO
            ),
            actions=[
                ft.TextButton(
                    content="Отмена",
                    on_click=lambda ev: page.pop_dialog()
                ),
                ft.Button(
                    content="Сохранить",
                    bgcolor="pink400",
                    color="white",
                    on_click=save_settings
                )
            ]
        )

        page.show_dialog(dialog)

    # ---------- Интерфейс ----------

    def build_chat_interface():
        nonlocal interface_ready

        settings_button = ft.IconButton(
            icon=ft.Icons.SETTINGS,
            icon_color="pink",
            tooltip="Настройки",
            on_click=open_settings
        )

        logout_button = ft.IconButton(
            icon=ft.Icons.LOGOUT,
            icon_color="pink",
            tooltip="Выйти из аккаунта",
            on_click=logout
        )

        async def search_users(e=None):
            query = (search_input.value or "").strip()

            if not websocket or not is_authenticated:
                return

            if query:
                await websocket.send(json.dumps({
                    "type": "search_user",
                    "query": query
                }))
            else:
                search_order.clear()

            render_sidebar()
            page.update()

        search_input.on_change = search_users
        message_input.on_submit = send_message

        send_button = ft.IconButton(
            icon=ft.Icons.SEND,
            icon_color="white",
            bgcolor="pink400",
            on_click=send_message
        )

        own_profile = ft.Container(
            content=ft.Row(
                [
                    self_avatar,
                    ft.Column(
                        [
                            self_name_text,
                            self_username_text
                        ],
                        spacing=1,
                        expand=True
                    ),
                    settings_button,
                    logout_button
                ],
                spacing=6
            ),
            padding=8,
            border_radius=10,
            bgcolor="black12"
        )

        sidebar = ft.Container(
            content=ft.Column(
                [
                    ft.Text(
                        "✨ Femboygram",
                        size=18,
                        weight=ft.FontWeight.BOLD,
                        color="pink"
                    ),
                    ft.Text(
                        "Вы авторизованы",
                        color="green",
                        size=12
                    ),
                    own_profile,
                    ft.Text(
                        "Поиск пользователей",
                        size=15,
                        weight=ft.FontWeight.BOLD
                    ),
                    search_input,
                    ft.Divider(),
                    users_column
                ],
                expand=True,
                spacing=10
            ),
            width=280,
            padding=12,
            bgcolor="black12"
        )

        chat_area = ft.Container(
            content=ft.Column(
                [
                    call_panel,
                    header_box,
                    ft.Divider(),
                    ft.Container(
                        content=chat_list,
                        expand=True,
                        bgcolor="black26",
                        border_radius=10,
                        padding=10
                    ),
                    ft.Row(
                        [
                            message_input,
                            send_button
                        ],
                        spacing=8
                    )
                ],
                expand=True,
                spacing=10
            ),
            expand=True,
            padding=12
        )

        page.add(
            ft.Row(
                [
                    sidebar,
                    ft.VerticalDivider(width=1),
                    chat_area
                ],
                expand=True,
                spacing=0
            )
        )

        page.title = "Femboygram — чаты"
        page.horizontal_alignment = ft.CrossAxisAlignment.STRETCH
        page.vertical_alignment = ft.MainAxisAlignment.START

        interface_ready = True

        render_sidebar()
        page.update()

    page.update()
    page.run_task(init_ws)


ft.run(main)
