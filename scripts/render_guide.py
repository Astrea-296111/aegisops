"""Optional documentation build helper; install Markdown to rebuild the HTML guide."""

from pathlib import Path

import markdown

root = Path(__file__).resolve().parents[1]
source = root / "docs/project-guide.md"
parser = markdown.Markdown(
    extensions=["tables", "fenced_code", "toc"],
    extension_configs={"toc": {"title": "阅读目录", "toc_depth": "2-2"}},
)
body = parser.convert(source.read_text(encoding="utf-8"))
html = (
    """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>AegisOps · 初学者项目说明书</title><style>
:root{--ink:#1b2b3c;--muted:#536575;--accent:#087f78;--line:#d8e3e7}*{box-sizing:border-box}body{margin:0;color:var(--ink);background:#f4f7f9;font-family:system-ui,"Microsoft YaHei","PingFang SC",sans-serif;font-size:16px;line-height:1.85}header{background:#102b39;color:white;padding:50px max(6vw,24px)}header small{color:#6fe0cb;letter-spacing:3px;font-weight:700}header h1{font-size:clamp(26px,4vw,44px);margin:.3em 0;line-height:1.3}header p{color:#c2d5df;margin:0}.layout{max-width:1300px;margin:auto;display:grid;grid-template-columns:250px minmax(0,1fr);gap:36px;padding:32px 24px}nav{position:sticky;top:24px;align-self:start;font-size:14px;max-height:92vh;overflow:auto}nav ul{padding-left:20px}nav a{color:var(--muted);text-decoration:none}nav a:hover{color:var(--accent)}main{background:white;padding:32px 42px;border:1px solid var(--line);border-radius:12px;min-width:0}main>h1{font-size:30px}h2{margin-top:2.1em;padding-top:1em;border-top:2px solid var(--line);color:#0b706b;font-size:24px;line-height:1.5;scroll-margin-top:24px}h3{font-size:19px;margin-top:1.6em}p{margin:.9em 0}a{color:#077d79}pre{padding:18px;background:#142d3a;color:#eef8fc;border-radius:8px;overflow:auto;font-size:13px;line-height:1.7}code{font-family:Consolas,monospace;overflow-wrap:anywhere}p code,li code,td code{background:#eaf2f3;padding:2px 5px;border-radius:4px;font-size:.9em}table{width:100%;border-collapse:collapse;font-size:14px;display:block;overflow:auto;margin:24px 0}th,td{border:1px solid var(--line);padding:11px 13px;vertical-align:top;min-width:110px}th{background:#e8f4f2;color:#115d59;text-align:left}tbody tr:nth-child(even){background:#f8fafb}li{margin:7px 0}strong{color:#0e615c}footer{color:var(--muted);font-size:13px;max-width:1000px;padding:20px 24px 50px;margin:auto}.badge{display:inline-block;padding:3px 12px;border:1px solid #446a75;border-radius:20px;margin-top:16px;font-size:13px}@media(max-width:850px){.layout{display:block;padding:14px}nav{position:static;max-height:none;margin-bottom:24px}main{padding:22px}header{padding:32px 24px}}@media print{body{background:white;font-size:10.5pt}header{padding:16px;color:black;background:white}header small,header p{color:#333}nav{display:none}.layout{display:block;padding:0}main{border:0;padding:0}pre{white-space:pre-wrap;background:#f5f5f5;color:black}h2,h3{break-after:avoid}table{font-size:9pt}tr{break-inside:avoid}footer{display:none}}
</style></head><body><header><small>AEGISOPS / ENGINEERING GUIDE</small><h1>从一次故障排查，读懂安全 Agent</h1><p>可恢复的执行 · 可追溯的证据 · 有边界的修复</p><div class="badge">Python 3.12 · Windows 入门路线 · v0.1.0</div></header><div class="layout"><nav>"""
    + parser.toc
    + """</nav><main>"""
    + body
    + """</main></div><footer>本说明书可离线阅读，无远端脚本或外部字体。实际执行与未验证项目请参阅随包 docs/verification.md。</footer></body></html>"""
)
(root / "docs/project-guide.html").write_text(html, encoding="utf-8")
print("Rendered project-guide.html")
