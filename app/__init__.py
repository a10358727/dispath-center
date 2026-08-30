"""Dispatch Center — Agent-native Engineering Platform

保持此檔案盡量空白，避免匯入需要外部服務（asyncssh 連線等）的模組，
讓 tests/ 內針對純函式的測試可以只匯入需要的子模組，而不必安裝/啟動整個
應用程式。
"""
