from pathlib import Path
import copy
import json
import os
import threading
import uuid


class DrawLog:
    """抽签批次与现场事件的本地持久化，供复查与沿原设计追溯。"""

    def __init__(self, directory):
        self.path = Path(directory) / 'draws.json'
        self.lock = threading.Lock()

    def _read(self):
        if not self.path.exists():
            return []
        return json.loads(self.path.read_text(encoding='utf-8'))

    def _write(self, records):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix('.' + uuid.uuid4().hex + '.tmp')
        try:
            with temporary.open('w', encoding='utf-8') as handle:
                json.dump(records, handle, ensure_ascii=False, allow_nan=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def append(self, record):
        with self.lock:
            records = self._read()
            records.append(copy.deepcopy(record))
            self._write(records)

    def update(self, record):
        with self.lock:
            records = self._read()
            for index, existing in enumerate(records):
                if existing['draw_id'] == record['draw_id']:
                    records[index] = copy.deepcopy(record)
                    self._write(records)
                    return
            raise ValueError('抽签记录不存在')

    def get(self, draw_id):
        with self.lock:
            for record in self._read():
                if record['draw_id'] == draw_id:
                    return copy.deepcopy(record)
            return None

    def list(self):
        with self.lock:
            return copy.deepcopy(self._read())
