"""Mac の通知。通知センター（UserNotifications）に許可されていればそれで、だめなら osascript。

2026-10-02 の試し: 署名なし（簡易署名）のアプリは、Mac が「このアプリの通知は許されていません」と即答し、
通知の設定の一覧にも載らない（利用者が許可する方法が無い）。なので実際には osascript（「スクリプトエディタ」名義）で出る。
"""
import os
import subprocess
import uuid

_center = None
_delegate = None
_allowed = False   # 通知センターに許可されたか（許可の返事は非同期で来る）


def setup(log) -> None:
    """通知の許可をもらう（初回だけ Mac が「通知を許可しますか」と聞く）"""
    global _center, _delegate
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
            log.info("通知の許可: %s %s", granted, err or "")

        _center.requestAuthorizationWithOptions_completionHandler_(
            UNAuthorizationOptionAlert | UNAuthorizationOptionSound, _answer)
    except Exception as e:   # ソースから動かしているとき（アプリの識別子が無い）など
        log.info("通知センターを使えないので osascript で出します: %s", e)
        _center = None


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
            return
        except Exception:
            pass
    esc = lambda s: s.replace("\\", "\\\\").replace('"', '\\"')  # noqa: E731
    subprocess.run(["osascript", "-e", f'display notification "{esc(body)}" with title "{esc(title)}" '
                    'subtitle "UGC Collector" sound name "default"'], capture_output=True)
