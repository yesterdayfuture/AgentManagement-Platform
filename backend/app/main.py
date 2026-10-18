"""进程入口。

uvicorn 加载的是这里的 app。真正的装配（数据库、种子数据、路由、前端静态文件）
都在 core.create_app 里，测试也可以直接调用 create_app 并换一套数据库。
"""

from app.core import create_app

app = create_app()
