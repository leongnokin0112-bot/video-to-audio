# 影片轉 MP3

FastAPI + FFmpeg，包含響應式上傳頁面、上傳進度、MP3 預覽及下載、大小限制與暫存清理。

此儲存庫為部署版：Dockerfile 將 main.py 放入 app/，index.html 放入 static/。Python 3.12，FFmpeg 由 Docker 自動安裝。

在 Render 使用 Public Git Repository 連接此儲存庫，Runtime 選 Docker，Instance Type 選 Free，Health Check 使用 /health，維持單 instance。

本機：docker build -t video-to-audio . 然後 docker run --rm -p 8000:8000 video-to-audio 。開啟 http://localhost:8000 。

預設最多 50 MiB，提取前 60 分鐘，轉換限時 180 秒。成功傳送、失敗或逾時清理暫存；每分鐘清理異常遺留且超過一小時的工作。微信可能限制下載，請使用外部瀏覽器。
