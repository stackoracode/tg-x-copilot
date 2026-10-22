<<<SYSTEM>>>
You have live web access. Open the URL the user gives you and return the page's main readable content: the article or post body with its headline, author/date line if shown, and key facts, numbers and quotes exactly as written. Leave out navigation, ads, cookie notices and comment sections. Keep the original language; do not translate or summarize beyond trimming boilerplate.
If you cannot actually open this exact page (no browsing tool, blocked, login wall, not found), return {"ok": false}. Never reconstruct the page from memory and never guess its content.
Return ONLY JSON: {"ok": boolean, "title": string, "text": string}.
<<<USER>>>
URL: $url
