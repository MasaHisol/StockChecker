# PyInstaller 設定: pyinstaller StockChecker.spec
from PyInstaller.utils.hooks import collect_all

pw_datas, pw_binaries, pw_hidden = collect_all("playwright")  # ブラウザ操作用ドライバ
msg_datas, msg_binaries, msg_hidden = collect_all("extract_msg")  # Outlook .msg の読み込み

a = Analysis(
    ["launcher.py"],
    datas=[("stockchecker/templates", "stockchecker/templates"),
           ("stockchecker/static", "stockchecker/static"),
           ("stockchecker/schema.sql", "stockchecker")] + pw_datas + msg_datas,
    binaries=pw_binaries + msg_binaries,
    hiddenimports=["waitress", "anthropic", "openpyxl"] + pw_hidden + msg_hidden,
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas, name="StockChecker", console=True, upx=False)
