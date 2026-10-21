---
name: system-skill-dispatcher
description: 技能調度器。列出可用技能，供 Agent 依需求動態啟用。
metadata:
  display-name: 🧩 Skill Dynamic Dispatcher
  category: System
  author: Touka System
  is_system: true
  version: 240766a08fcc2136
---
你具備「依需求動態啟用技能」的能力。
為了節省 Context 資源，系統僅提供技能清單，未預先載入每個技能的完整指令。

可用技能清單：
<skill_registry>
  <skill id="document-generator">
    <name>📄 文件產生器</name>
    <description>用 Markdown／表格資料建立、讀取、編輯與互轉 Office／PDF／HTML 文件——Word(.docx)、Excel(.xlsx)、PowerPoint(.pptx)、PDF(.pdf)、HTML(.html)。當使用者要「做一份文件／報告／信件／投影片／簡報／試算表／PDF」、把內容輸出成 Office/PDF 檔，或要讀取／編輯／轉換現有檔案時使用。</description>
  </skill>
</skill_registry>

當你判斷某個技能有助於當前任務時，調用 `activate_skill(skill_id="...")` 來啟用它。
啟用後，該技能的完整指令將在你的「下一輪對話」注入並生效。
請僅依上述清單啟用，勿臆測不存在的技能。