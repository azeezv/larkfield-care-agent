import json
from src.extract import extract

tickets = {t['ticket_id']: t for t in json.load(open('dataset/tickets/support_inbox_export.json'))}

for tid in ['T002','T019','T020','T022']:
    print(tid, json.dumps(extract(tickets[tid]), indent=1, default=str))