"""Send one fresh EEG control result per packet to the local ball controller."""

import json
import math
import socket
import uuid


class ScoreSender:
    def __init__(self, host="127.0.0.1", port=50005):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.target = (host, port)
        self.session_id = uuid.uuid4().hex
        self.seq = 0

    def send(self, score=None, valid=False, status="calibrating", event=None):
        if valid:
            score = float(score)
            if not math.isfinite(score) or not 0 <= score <= 100:
                raise ValueError("score must be finite and between 0 and 100")
        else:
            score = None
            event = None
        self.seq += 1
        packet = {"version": 1, "session_id": self.session_id,
                  "seq": self.seq, "score": score, "valid": bool(valid),
                  "status": status, "event": event}
        self.sock.sendto(json.dumps(packet, allow_nan=False).encode("utf-8"), self.target)

    def close(self):
        self.sock.close()
