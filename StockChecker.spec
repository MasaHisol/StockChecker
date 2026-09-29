# PyInstaller 設定: pyinstaller StockChecker.spec
a = Analysis(
    ["launcher.py"],
    datas=[("stockchecker/templates", "stockchecker/templates"),
           ("stockchecker/schema.sql", "stockchecker")],
    hiddenimports=["waitress", "anthropic"],
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas, name="StockChecker", console=True, upx=False)
