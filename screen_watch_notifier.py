# -*- coding: utf-8 -*-
"""
화면 변화 감지 알림 프로그램 (Screen Watch Notifier)
----------------------------------------------------
- 사용자가 지정한 화면 영역을 주기적으로 캡처하여 이전 캡처와 비교
- 변화량이 설정된 임계값(threshold)을 넘으면 "알림"만 발생 (자동 클릭 없음)
- 예: 예매 버튼이 비활성 -> 활성 상태로 바뀌는 순간을 사람에게 알려줌

필요 패키지 설치:
    pip install mss pillow numpy plyer

실행:
    python screen_watch_notifier.py
"""

import tkinter as tk
from tkinter import ttk, messagebox
import threading
import time
import datetime
import sys

import numpy as np
from PIL import Image, ImageTk, ImageEnhance

try:
    import mss
except ImportError:
    mss = None

try:
    from plyer import notification as plyer_notification
except ImportError:
    plyer_notification = None

if sys.platform.startswith("win"):
    import winsound

# 운영체제별로 한글이 안정적으로 표시되는 폰트를 사용한다.
# ("맑은 고딕"은 윈도우 전용 폰트라 macOS/Linux에서는 다른 폰트로 대체)
if sys.platform == "darwin":
    FONT_NAME = "AppleGothic"
elif sys.platform.startswith("win"):
    FONT_NAME = "맑은 고딕"
else:
    FONT_NAME = "NanumGothic"


# ----------------------------------------------------------------------
# 영역 드래그 지정용 오버레이 창
# ----------------------------------------------------------------------
class RegionSelector(tk.Toplevel):
    """
    화면을 미리 찍은 스냅샷을 전체 화면에 깔고, 그 위에서 마우스 드래그로 영역을 지정받는다.
    (반투명 창 방식은 macOS에서 뒤 화면이 안 보이는 문제가 있어 스냅샷 방식을 사용)
    """

    def __init__(self, master, screenshot, on_complete):
        super().__init__(master)
        self.on_complete = on_complete
        self.finished = False

        screen_w = self.winfo_screenwidth()
        screen_h = self.winfo_screenheight()

        # 스냅샷을 화면 크기(tk 좌표 단위)에 맞게 줄이고 살짝 어둡게 만든다.
        # (Retina 화면은 캡처 이미지가 2배 크기라 리사이즈가 필요하다)
        img = screenshot.resize((screen_w, screen_h))
        img = ImageEnhance.Brightness(img).enhance(0.6)
        self.bg_image = ImageTk.PhotoImage(img)  # 참조를 유지해야 이미지가 사라지지 않는다

        self.geometry(f"{screen_w}x{screen_h}+0+0")
        self.overrideredirect(True)   # 제목표시줄/창 테두리 제거
        self.attributes("-topmost", True)

        self.canvas = tk.Canvas(
            self, width=screen_w, height=screen_h,
            highlightthickness=0, cursor="crosshair"
        )
        self.canvas.pack(fill="both", expand=True)
        self.canvas.create_image(0, 0, image=self.bg_image, anchor="nw")

        # 안내 문구
        self.canvas.create_rectangle(
            screen_w // 2 - 260, 30, screen_w // 2 + 260, 74, fill="black", outline=""
        )
        self.canvas.create_text(
            screen_w // 2, 52, fill="white", font=(FONT_NAME, 14),
            text="마우스를 드래그하여 감지할 영역을 지정하세요  (ESC: 취소)"
        )

        self.start_x = None
        self.start_y = None
        self.rect_id = None

        self.canvas.bind("<ButtonPress-1>", self.on_press)
        self.canvas.bind("<B1-Motion>", self.on_drag)
        self.canvas.bind("<ButtonRelease-1>", self.on_release)
        self.bind("<Escape>", lambda e: self._finish(None))

        self.lift()
        self.focus_force()

    def _finish(self, region):
        # 어떤 경우든(취소/완료/너무 작은 영역) 메인 창이 다시 나타나도록 콜백을 항상 호출한다.
        if self.finished:
            return
        self.finished = True
        self.destroy()
        self.on_complete(region)

    def on_press(self, event):
        self.start_x, self.start_y = event.x, event.y
        if self.rect_id:
            self.canvas.delete(self.rect_id)
        self.rect_id = self.canvas.create_rectangle(
            self.start_x, self.start_y, self.start_x, self.start_y,
            outline="#4C6FFF", width=2
        )

    def on_drag(self, event):
        if self.rect_id:
            self.canvas.coords(self.rect_id, self.start_x, self.start_y, event.x, event.y)

    def on_release(self, event):
        if self.start_x is None:
            self._finish(None)
            return
        x1, y1 = self.start_x, self.start_y
        x2, y2 = event.x, event.y
        left, top = min(x1, x2), min(y1, y2)
        width, height = abs(x2 - x1), abs(y2 - y1)

        if width < 5 or height < 5:
            self._finish(None)  # 너무 작은 영역은 무시
            return

        self._finish({"left": left, "top": top, "width": width, "height": height})


# ----------------------------------------------------------------------
# 메인 애플리케이션
# ----------------------------------------------------------------------
class ScreenWatchApp:
    STATUS_IDLE = "대기 중"
    STATUS_REGION_SET = "영역 지정 완료"
    STATUS_WATCHING = "감지 중..."
    STATUS_CHANGED = "화면 변화 감지"
    STATUS_NOTIFIED = "알림 발생"
    STATUS_STOPPED = "중지됨"

    def __init__(self, root):
        self.root = root
        self.root.title("화면 변화 감지 알림 프로그램")
        self.root.geometry("880x640")
        self.root.configure(bg="#F4F6FB")

        self.region = None          # {"left","top","width","height"}
        self.watch_thread = None
        self.watching = False
        self.prev_frame = None

        # 설정값 (Tk 변수)
        self.sensitivity_var = tk.DoubleVar(value=5.0)   # 변화율(%) 임계값
        self.interval_var = tk.DoubleVar(value=0.5)       # 캡처 주기(초)
        self.auto_stop_var = tk.BooleanVar(value=True)    # 알림 후 자동 중지
        self.sound_var = tk.BooleanVar(value=True)        # 소리 알림 여부

        self._build_ui()
        self._set_status(self.STATUS_IDLE)
        self._log("프로그램 준비 완료")

    # ---------------- UI 구성 ----------------
    def _build_ui(self):
        header = tk.Frame(self.root, bg="#F4F6FB")
        header.pack(fill="x", padx=24, pady=(20, 10))

        tk.Label(
            header, text="🔔 화면 변화 감지 알림 프로그램",
            font=(FONT_NAME, 18, "bold"), bg="#F4F6FB", fg="#1F2430"
        ).pack(anchor="w")
        tk.Label(
            header, text="지정한 영역의 변화(예: 예매 버튼 활성화)를 감지하면 자동 클릭 대신 알림을 드립니다.",
            font=(FONT_NAME, 10), bg="#F4F6FB", fg="#6B7280"
        ).pack(anchor="w", pady=(2, 0))

        body = tk.Frame(self.root, bg="#F4F6FB")
        body.pack(fill="both", expand=True, padx=24, pady=10)
        body.columnconfigure(0, weight=1)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(1, weight=1)

        # ---- 좌측: 영역 지정 + 실행 ----
        left = tk.Frame(body, bg="#F4F6FB")
        left.grid(row=0, column=0, rowspan=2, sticky="nsew", padx=(0, 12))

        self._build_region_card(left)
        self._build_run_card(left)
        self._build_settings_card(left)

        # ---- 우측: 상태 + 로그 ----
        right = tk.Frame(body, bg="#F4F6FB")
        right.grid(row=0, column=1, rowspan=2, sticky="nsew")

        self._build_status_card(right)
        self._build_log_card(right)

    def _card(self, parent, title, subtitle=None):
        card = tk.Frame(parent, bg="white", highlightbackground="#E5E7EB", highlightthickness=1)
        card.pack(fill="x", pady=(0, 12))
        inner = tk.Frame(card, bg="white")
        inner.pack(fill="both", expand=True, padx=16, pady=14)
        tk.Label(inner, text=title, font=(FONT_NAME, 12, "bold"), bg="white", fg="#1F2430").pack(anchor="w")
        if subtitle:
            tk.Label(inner, text=subtitle, font=(FONT_NAME, 9), bg="white", fg="#8A8F98").pack(anchor="w", pady=(2, 8))
        else:
            tk.Frame(inner, height=6, bg="white").pack()
        return inner

    def _build_region_card(self, parent):
        inner = self._card(parent, "① 예매 버튼 영역 지정", "감지할 영역을 드래그로 지정하세요.")

        self.region_info_label = tk.Label(
            inner, text="지정된 영역이 없습니다.",
            font=(FONT_NAME, 10), bg="white", fg="#374151", justify="left"
        )
        self.region_info_label.pack(anchor="w", pady=(0, 10))

        btn_row = tk.Frame(inner, bg="white")
        btn_row.pack(fill="x")

        self.capture_btn = tk.Button(
            btn_row, text="📐  영역 캡처하기", font=(FONT_NAME, 10, "bold"),
            bg="#4C6FFF", fg="white", activebackground="#3D5AE0", bd=0, pady=8,
            command=self.start_region_capture
        )
        self.capture_btn.pack(side="left", fill="x", expand=True, padx=(0, 6))

        self.reset_btn = tk.Button(
            btn_row, text="♻ 영역 다시 지정", font=(FONT_NAME, 10),
            bg="#F3F4F6", fg="#374151", bd=0, pady=8,
            command=self.start_region_capture
        )
        self.reset_btn.pack(side="left", fill="x", expand=True, padx=(6, 0))

    def _build_run_card(self, parent):
        inner = self._card(parent, "② 실행", "설정이 완료되면 감지를 시작하세요.")

        self.run_btn = tk.Button(
            inner, text="▶  감지 시작", font=(FONT_NAME, 11, "bold"),
            bg="#22C55E", fg="white", bd=0, pady=10,
            command=self.start_watching
        )
        self.run_btn.pack(fill="x", pady=(0, 6))

        self.stop_btn = tk.Button(
            inner, text="■  중지", font=(FONT_NAME, 10),
            bg="#F3F4F6", fg="#374151", bd=0, pady=8,
            command=self.stop_watching, state="disabled"
        )
        self.stop_btn.pack(fill="x")

    def _build_settings_card(self, parent):
        inner = self._card(parent, "⚙ 추가 설정", None)

        def row(label):
            r = tk.Frame(inner, bg="white")
            r.pack(fill="x", pady=4)
            tk.Label(r, text=label, font=(FONT_NAME, 9), bg="white", fg="#374151", width=16, anchor="w").pack(side="left")
            return r

        r1 = row("변화 감지 민감도 (%)")
        tk.Scale(r1, from_=0.5, to=30, resolution=0.5, orient="horizontal",
                 variable=self.sensitivity_var, bg="white", highlightthickness=0, length=160).pack(side="left")

        r2 = row("화면 확인 주기 (초)")
        tk.Scale(r2, from_=0.1, to=3.0, resolution=0.1, orient="horizontal",
                 variable=self.interval_var, bg="white", highlightthickness=0, length=160).pack(side="left")

        r3 = row("소리 알림")
        tk.Checkbutton(r3, text="켜기", variable=self.sound_var, bg="white", font=(FONT_NAME, 9)).pack(side="left")

        r4 = row("알림 후 자동 중지")
        tk.Checkbutton(r4, text="켜기", variable=self.auto_stop_var, bg="white", font=(FONT_NAME, 9)).pack(side="left")

        tk.Label(
            inner,
            text="※ 민감도가 낮을수록(%) 작은 변화에도 민감하게 반응합니다.\n   미세한 애니메이션 오작동이 잦다면 값을 높여주세요.",
            font=(FONT_NAME, 8), bg="white", fg="#9CA3AF", justify="left"
        ).pack(anchor="w", pady=(8, 0))

    def _build_status_card(self, parent):
        inner = self._card(parent, "현재 상태", None)
        self.status_dot = tk.Label(inner, text="●", font=(FONT_NAME, 14), bg="white", fg="#9CA3AF")
        self.status_dot.pack(side="left")
        self.status_label = tk.Label(inner, text=self.STATUS_IDLE, font=(FONT_NAME, 13, "bold"), bg="white", fg="#1F2430")
        self.status_label.pack(side="left", padx=(6, 0))

    def _build_log_card(self, parent):
        card = tk.Frame(parent, bg="white", highlightbackground="#E5E7EB", highlightthickness=1)
        card.pack(fill="both", expand=True)
        inner = tk.Frame(card, bg="white")
        inner.pack(fill="both", expand=True, padx=16, pady=14)

        tk.Label(inner, text="📋 실행 로그", font=(FONT_NAME, 12, "bold"), bg="white", fg="#1F2430").pack(anchor="w")

        log_frame = tk.Frame(inner, bg="white")
        log_frame.pack(fill="both", expand=True, pady=(8, 0))

        scrollbar = tk.Scrollbar(log_frame)
        scrollbar.pack(side="right", fill="y")

        self.log_text = tk.Text(
            log_frame, height=16, font=("Consolas", 9), bg="#F9FAFB", fg="#374151",
            bd=0, yscrollcommand=scrollbar.set, wrap="word", state="disabled"
        )
        self.log_text.pack(side="left", fill="both", expand=True)
        scrollbar.config(command=self.log_text.yview)

    # ---------------- 유틸 ----------------
    def _log(self, message):
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"[{ts}] {message}\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _set_status(self, status):
        colors = {
            self.STATUS_IDLE: "#9CA3AF",
            self.STATUS_REGION_SET: "#4C6FFF",
            self.STATUS_WATCHING: "#F59E0B",
            self.STATUS_CHANGED: "#EF4444",
            self.STATUS_NOTIFIED: "#EF4444",
            self.STATUS_STOPPED: "#6B7280",
        }
        self.status_label.config(text=status)
        self.status_dot.config(fg=colors.get(status, "#9CA3AF"))

    # ---------------- 영역 지정 ----------------
    def start_region_capture(self):
        if mss is None:
            messagebox.showerror("오류", "mss 패키지가 필요합니다.\n\npip install mss")
            return
        self.root.withdraw()  # 메인 창 숨기기
        # 메인 창이 완전히 사라진 뒤에 화면을 찍어야 하므로 잠깐 기다린다.
        self.root.after(400, self._open_selector)

    def _open_selector(self):
        try:
            with mss.mss() as sct:
                shot = sct.grab(sct.monitors[1])  # 주 모니터 전체
                screenshot = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
            RegionSelector(self.root, screenshot, self._on_region_selected)
        except Exception as e:
            self.root.deiconify()
            self._log(f"영역 지정 화면을 여는 중 오류: {e}")

    def _on_region_selected(self, region):
        self.root.deiconify()  # 메인 창 다시 표시
        if region is None:
            self._log("영역 지정이 취소되었습니다.")
            return

        self.region = region
        info = (
            f"X: {region['left']}, Y: {region['top']}   "
            f"가로: {region['width']}px, 세로: {region['height']}px"
        )
        self.region_info_label.config(text=info)
        self._set_status(self.STATUS_REGION_SET)
        self._log(f"캡처 영역이 지정되었습니다. ({info})")

    # ---------------- 감지 실행 ----------------
    def start_watching(self):
        if mss is None:
            messagebox.showerror("오류", "mss 패키지가 필요합니다.\n\npip install mss")
            return
        if not self.region:
            messagebox.showwarning("알림", "먼저 감지할 영역을 지정해주세요.")
            return

        self.watching = True
        self.prev_frame = None
        self.run_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.capture_btn.config(state="disabled")
        self.reset_btn.config(state="disabled")

        self._set_status(self.STATUS_WATCHING)
        self._log("감지를 시작합니다.")

        self.watch_thread = threading.Thread(target=self._watch_loop, daemon=True)
        self.watch_thread.start()

    def stop_watching(self, notify_log=True):
        self.watching = False
        self.run_btn.config(state="normal")
        self.stop_btn.config(state="disabled")
        self.capture_btn.config(state="normal")
        self.reset_btn.config(state="normal")
        self._set_status(self.STATUS_STOPPED)
        if notify_log:
            self._log("감지를 중지했습니다.")

    def _watch_loop(self):
        interval = max(0.05, self.interval_var.get())
        threshold = self.sensitivity_var.get()  # % 단위

        with mss.mss() as sct:
            monitor = {
                "left": self.region["left"],
                "top": self.region["top"],
                "width": self.region["width"],
                "height": self.region["height"],
            }
            while self.watching:
                frame = self._capture(sct, monitor)

                if self.prev_frame is not None:
                    change_ratio = self._diff_ratio(self.prev_frame, frame)
                    if change_ratio >= threshold:
                        self._on_change_detected(change_ratio)
                        if self.auto_stop_var.get():
                            self.root.after(0, lambda: self.stop_watching(notify_log=False))
                            self.root.after(0, lambda: self._log("알림 후 자동으로 감지를 중지했습니다."))
                            break
                    else:
                        self.root.after(0, lambda: self._set_status(self.STATUS_WATCHING))

                self.prev_frame = frame
                time.sleep(interval)

    def _capture(self, sct, monitor):
        shot = sct.grab(monitor)
        img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
        arr = np.array(img.convert("L"), dtype=np.float32)  # 그레이스케일로 변환 후 비교
        return arr

    def _diff_ratio(self, prev, curr):
        """두 프레임 간 변화 비율(%)을 계산한다."""
        if prev.shape != curr.shape:
            return 0.0
        diff = np.abs(curr - prev)
        # 픽셀 값 차이가 일정 수준(20/255) 이상인 픽셀만 '변화'로 카운트하여
        # 미세한 노이즈나 애니메이션에 의한 오탐을 줄인다.
        changed_pixels = np.sum(diff > 20)
        total_pixels = diff.size
        return (changed_pixels / total_pixels) * 100.0

    def _on_change_detected(self, change_ratio):
        self.root.after(0, lambda: self._set_status(self.STATUS_CHANGED))
        self.root.after(0, lambda: self._log(f"지정 영역의 변화가 감지되었습니다. (변화율 {change_ratio:.1f}%)"))
        self.root.after(0, self._fire_notification)

    def _fire_notification(self):
        self._set_status(self.STATUS_NOTIFIED)
        self._log("알림 이벤트를 실행합니다.")

        # 1) 소리 알림
        if self.sound_var.get():
            try:
                if sys.platform.startswith("win"):
                    winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
                else:
                    self.root.bell()
            except Exception:
                pass

        # 2) 시스템 알림 (plyer 사용, 없으면 팝업으로 대체)
        title = "예매 버튼 감지"
        message = "지정한 영역에서 변화가 감지되었습니다. 지금 확인해보세요!"
        notified = False
        if plyer_notification is not None:
            try:
                plyer_notification.notify(title=title, message=message, timeout=6)
                notified = True
            except Exception:
                notified = False

        # 3) 항상 눈에 띄는 팝업도 함께 띄운다 (플랫폼 알림이 없을 경우 대비)
        self._show_popup(title, message)

    def _show_popup(self, title, message):
        popup = tk.Toplevel(self.root)
        popup.attributes("-topmost", True)
        popup.title(title)
        popup.configure(bg="white")
        popup.geometry("320x140+80+80")

        tk.Label(popup, text=f"🔔 {title}", font=(FONT_NAME, 13, "bold"), bg="white", fg="#EF4444").pack(pady=(16, 6))
        tk.Label(popup, text=message, font=(FONT_NAME, 10), bg="white", fg="#374151", wraplength=280, justify="center").pack()
        tk.Button(popup, text="확인", command=popup.destroy, bg="#4C6FFF", fg="white", bd=0, padx=20, pady=6).pack(pady=14)

        popup.after(8000, lambda: popup.destroy() if popup.winfo_exists() else None)


def main():
    root = tk.Tk()
    app = ScreenWatchApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
