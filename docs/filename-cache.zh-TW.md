# 常駐檔名快取（2026-09-24）

## 實作

`storage.py` 移除 request-triggered TTL cache，改用 `catalog_index.py` 的常駐不可變快照。
建立 `MemeStorage` 時先完成暖機，之後只由一個定時維護執行緒重掃。
不修改 `combined_bot.py` 的指令、文字與圖片品質；三個 bot 入口都使用相同 storage。
獨立入口原有的 `storage.refresh()` 啟動呼叫仍可使用（會額外做一次明確更新）。

- 預設每 900 秒更新一次；每次更新完成後才開始下一段等待，不累積重掃工作。
- 更新在旁邊建新版本，三個目錄的所有分頁成功後才一次替換。
- 更新逾時、API 失敗或分頁失敗：保留上一份完整快照，下一週期再試。
- 讀取不取更新鎖、不檢查 TTL、不觸發 S3 LIST；更新卡住也可讀舊索引。
- `memes` / `airou` 回傳同一份 tuple，不為每個請求複製整份檔名清單。
- `YN` 的小清單刻意回傳 list 副本，因既有 Telegram `can_i` 會 `remove()`，不能讓它改掉共用索引。
- 未變動的目錄重用原 tuple；更新時會短暫同時存在新舊資料，進行中的讀者也可能暫時持有舊版。
- 沒有加入依使用者或關鍵字無限增長的搜尋結果快取。

## 設定

```text
S3_INDEX_REFRESH_SECONDS=900
```

不設定就是 15 分鐘，必須是有限正數。原先的 `S3_INDEX_TTL` 不再參與此機制。
新增/刪除圖檔在下一次成功更新後生效；正常約最多一個更新週期加上掃描時間。
持續更新失敗則繼續用舊版。啟動時無法暖機會直接報錯，不假裝圖庫為空。
快取期間已被刪除的圖片仍可能在下載時出現 NoSuchKey，讀取不會因此發起整個 bucket 重掃。

## 範圍與取捨

移除的是每次搜尋列舉遠端檔名，不是取消所有 S3 流量。
圖片下載、收圖儲存、原本的 bucket log 寫入仍會連 S3。
文字子字串比對仍是 RAM 內的 O(N)，不是 O(1) 搜尋。
初始化會等待一次图庫掃描。背景維護使用一個 daemon thread；正常程序離開會停止排程，顯式 `storage.close()` 最多等目前 worker 5 秒。
保留原本的 log 輪替，沒有變更 Railway restart workflow。
常駐快取會佔用檔名與一個維護執行緒的記憶體，換取少做遠端 LIST、少建暫存物件；不能據此保證整個程序 RSS 必然下降。

## 本機驗證

```bash
python -m unittest discover -s tests -v
python -m compileall -q storage.py catalog_index.py tests
```

24 項離線測試通過：多分頁/子目錄、空目錄、Airou fallback、1000 輪讀取無新增 LIST、刷新阻塞不阻塞讀取、失敗不發布半份快照、背景失敗後恢復、YN 修改隔離、重複啟動只有一個 worker、關閉與物件釋放。
使用真實 storage class / boto3 import，S3 回應為 fake，沒有正式 bot token 或 bucket credentials。

`cache-benchmark.json` 是 Python 3.13.5 的離線微測試：10,000 個人工檔名，暖機後 1,000 次搜尋，新增 S3 LIST 為 0；檔名 tuple + 字串物件為 1,060,040 bytes。
這不含 SDK、thread stack、配置器與 OS cache，不能當成 Railway RSS 或整體 RAM 減幅。
尚未在 Railway 與 Discord/Telegram 真實服務上跑端到端測試。

### 重現微測試

```python
import sys
import time
import tracemalloc
from unittest.mock import patch
sys.path.insert(0, 'tests')
from test_catalog_cache import FakeS3, ENV
from storage import MemeStorage

fake = FakeS3()
fake.keys = [f'memes/S{i:05d}_海綿寶寶_test.jpg' for i in range(10000)] + [
    'memes/YN/可以.jpg', 'airou/cat.jpg',
]
with patch.dict('os.environ', ENV, clear=True), patch('storage.boto3.client', return_value=fake):
    store = MemeStorage()
    try:
        baseline = len(fake.calls)
        names = store.list_memes()
        size = sys.getsizeof(names) + sum(sys.getsizeof(x) for x in names)
        tracemalloc.start()
        start = time.perf_counter()
        for _ in range(1000):
            assert store.list_memes() is names
            assert len(store.search_memes('S09999')) == 1
        elapsed = time.perf_counter() - start
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        print(dict(additional_list_calls=len(fake.calls)-baseline,
                   filename_bytes=size, peak_bytes=peak, seconds=elapsed))
    finally:
        store.close()
```

官方 S3 分頁 / Delimiter 文件：
https://docs.aws.amazon.com/boto3/latest/reference/services/s3/paginator/ListObjectsV2.html
