# PyInstaller 設定: pyinstaller StockChecker.spec
from PyInstaller.utils.hooks import collect_all

pw_datas, pw_binaries, pw_hidden = collect_all("playwright")  # ブラウザ操作用ドライバ

a = Analysis(
    ["launcher.py"],
    datas=[("stockchecker/templates", "stockchecker/templates"),
           ("stockchecker/static", "stockchecker/static"),
           ("stockchecker/schema.sql", "stockchecker")] + pw_datas,
    binaries=pw_binaries,
    hiddenimports=["waitress", "anthropic"] + pw_hidden,
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas, name="StockChecker", console=True, upx=False)
