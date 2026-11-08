# Sales Library files

Each `*.json` file here is one learned video, shown on **Tools › Sales Library** (`sales_library.py`).

The files are written and published by Avelino's Claude Code skills (`sales-video-learner` for YouTube, `sales-instagram-learner` for Instagram) with their `publish_to_library.py` script. That script validates the file and commits only that file to `main`, and the push deploys it. Hand edits are fine; keep the format below. The page skips any file that doesn't validate and tells Admins why.

```json
{
  "id": "guZa7mQV1l0",                 // file name = <id>.json; Instagram ids start with "ig-"
  "source": "youtube",                 // youtube | instagram
  "url": "https://www.youtube.com/watch?v=guZa7mQV1l0",
  "title": "Never Split the Difference | Chris Voss | Talks at Google",
  "creator": "Talks at Google",
  "length_seconds": 3042,
  "learned": "2026-11-07",
  "added_by": "Avelino",
  "summary": "3-5 sentences",
  "takeaways": ["…", "…", "…"],
  "lessons": [
    {
      "topic": "openers",              // openers rapport discovery value objections closing cancellations follow_up mindset
      "title": "Ask 'Is now a bad time?'",
      "how": "How it works and when to use it.",
      "say": ["A line a rep can say out loud"],
      "channels": ["outbound"],        // inbound outbound door_to_door
      "start_seconds": 310,            // null if unknown; YouTube links jump here
      "builds_on": "",                 // title of an earlier lesson this adds to, or ""
      "example": {                     // REQUIRED: a realistic Planet Networks fiber scene
        "setting": "Outbound call to a homeowner in a town where our fiber just went live; they have cable today.",
        "lines": [
          {"speaker": "Rep", "text": "…"},
          {"speaker": "Customer", "text": "…"}
        ]
      }
    }
  ],
  "objections": [{"objection": "…", "response": "…", "start_seconds": 139}],
  "applies": {"inbound": "…", "outbound": "…", "door_to_door": "…"},
  "doesnt_fit": [{"point": "…", "why": "…"}],
  "drill": {"goal": "…", "customer_script": "…", "debrief": "…"}
}
```

Rules: paraphrase (no long transcript quotes), and never invent Planet Networks prices, speeds, promos, contract terms or install timelines. Write `[fill in: …]` instead.
