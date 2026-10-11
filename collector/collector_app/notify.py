"""Mac の通知。通知センター（UserNotifications）に許可されていればそれで、だめなら osascript。

2026-10-02 の試し: 署名なし（簡易署名）のアプリは、Mac が「このアプリの通知は許されていません」と即答し、
通知の設定の一覧にも載らない（利用者が許可する方法が無い）。なので実際には osascript（「スクリプトエディタ」名義）で出る。

記録の書き方（2026-10-12）: 断られたときの Mac の英語の返事（Notifications are not allowed …）をそのまま書いていたら、
友達の Claude がそれを読んで「通知の設定で UGC Analyzer を許可して」と、できない案内をした。想定どおりだと分かる日本語で書き、
英語の文は書かない（エラーの番号だけ）。通知を出すたびに、出し方と題を1行残す（届かないと言われたときに確かめられるように）
"""
import logging
import os
import subprocess
import uuid

from . import config

_center = None
_delegate = None
_allowed = False   # 通知センターに許可されたか（許可の返事は非同期で来る）
_log = logging.getLogger("collector")   # setup() で、アプリの記録（collector.log）に差し替える
OSASCRIPT_NOTE = "通知はスクリプトエディタの名前で出す"


def setup(log) -> None:
    """通知の許可をもらう（初回だけ Mac が「通知を許可しますか」と聞く）"""
    global _center, _delegate, _log
    _log = log
    if os.environ.get("UGC_COLLECTOR_TEST_NO_CHROME"):   # 試験では通知の許可を聞かない（osascript で出す）
        return
    try:
        import objc
        from Foundation import NSObject
        from UserNotifications import (UNAuthorizationOptionAlert, UNAuthorizationOptionSound,
                                       UNNotificationPresentationOptionBanner, UNNotificationPresentationOptionList,
                                       UNNotificationPresentationOptionSound, UNUserNotificationCenter)

        class _Delegate(NSObject):
            # アプリが前面にいても通知を出す（メニューバーのアプリは前面扱いになることがある）
            def userNotificationCenter_willPresentNotification_withCompletionHandler_(self, center, n, handler):
                handler(UNNotificationPresentationOptionBanner | UNNotificationPresentationOptionList
                        | UNNotificationPresentationOptionSound)
            userNotificationCenter_willPresentNotification_withCompletionHandler_ = objc.selector(
                userNotificationCenter_willPresentNotification_withCompletionHandler_,
                signature=b"v@:@@@?")

        _center = UNUserNotificationCenter.currentNotificationCenter()
        _delegate = _Delegate.alloc().init()
        _center.setDelegate_(_delegate)
        def _answer(granted, err):
            global _allowed
            _allowed = bool(granted)
            log.info(answer_text(granted, err))

        _center.requestAuthorizationWithOptions_completionHandler_(
            UNAuthorizationOptionAlert | UNAuthorizationOptionSound, _answer)
    except Exception as e:   # ソースから動かしているとき（アプリの識別子が無い）など
        log.info("通知センターは使えない（%s。ソースから動かしているときなど、想定どおり）。%s", type(e).__name__, OSASCRIPT_NOTE)
        _center = None


def answer_text(granted, err) -> str:
    """通知センターの許可の返事の記録の文。断られたのは署名していないアプリだから（想定どおり。利用者が直せることは無い）"""
    if granted:
        return "通知センターに許可されました（通知は UGC Analyzer の名前で出す）"
    code = ""
    try:
        code = f"{err.domain()} {err.code()}" if err is not None else ""
    except Exception:
        code = type(err).__name__
    return (f"通知センターは使えない（署名していないアプリのため。想定どおりで、直すことは無い）。{OSASCRIPT_NOTE}"
            + (f"（Mac の返事: {code}）" if code else ""))


def send(title: str, body: str) -> None:
    if _center is not None and _allowed:
        try:
            from UserNotifications import UNMutableNotificationContent, UNNotificationRequest, UNNotificationSound
            c = UNMutableNotificationContent.alloc().init()
            c.setTitle_(title)
            c.setBody_(body)
            c.setSound_(UNNotificationSound.defaultSound())
            req = UNNotificationRequest.requestWithIdentifier_content_trigger_(str(uuid.uuid4()), c, None)
            _center.addNotificationRequest_withCompletionHandler_(req, None)
            _log.info("通知（通知センター）: %s", title)
            return
        except Exception as e:
            _log.info("通知センターで出せなかったので osascript で出します（%s）", type(e).__name__)
    esc = lambda s: s.replace("\\", "\\\\").replace('"', '\\"')  # noqa: E731
    try:
        r = subprocess.run(["osascript", "-e", f'display notification "{esc(body)}" with title "{esc(title)}" '
                            f'subtitle "{config.APP_NAME}" sound name "default"'], capture_output=True, text=True, timeout=30)
    except Exception as e:
        _log.warning("通知を出せませんでした（osascript が動かない: %s）: %s", type(e).__name__, title)
        return
    if r.returncode:
        _log.warning("通知を出せませんでした（osascript の終わり方 %s）: %s", r.returncode, title)
    else:
        _log.info("通知（osascript・スクリプトエディタの名前）: %s", title)
