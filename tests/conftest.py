import sys
import os
# テスト実行時にプロジェクトルートを sys.path に追加して `server` パッケージを見つけられるようにする
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
