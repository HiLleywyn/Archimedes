-- tasks.lua -- the Tasks plugin for Archimedes.
--
-- Tasks organised into named lists, backed by the shared `productivity`
-- document store. Tasks can carry a due date and a reminder; a one-minute
-- loop DMs the owner (or every group member) when a reminder falls due.
--
-- Standalone plugin: registers the `.task` command group and a reminder
-- loop. It shares the `productivity` namespace with notes, events and groups.

local PAGE = 10

local M = {}

M.manifest = {
  id = "tasks",
  name = "Tasks",
  version = "1.0.0",
  description = "Tasks and to-do lists, with reminders, sharing and groups.",
  author = "HiLleywyn",
  category = "Productivity",
  storage = "productivity",
}

local TIME_HINT = "Could not read that time. Try `in 2h`, `in 3d`, or "
  .. "`2026-06-01 14:30`."

-- ── helpers ──────────────────────────────────────────────────────────────────
local function trim(s)
  return (s or ""):gsub("^%s+", ""):gsub("%s+$", "")
end

local function is_member(grp, user_id)
  for _, uid in ipairs(grp.members or {}) do
    if uid == user_id then return true end
  end
  return false
end

local function scope_for(ctx, group_id)
  if not group_id then
    return "user", ctx.author_id
  end
  local grp = ctx.store.get("groups", group_id)
  if not grp then
    return nil, nil, "There is no group #" .. group_id .. "."
  end
  if not is_member(grp, ctx.author_id) then
    return nil, nil, "You are not a member of group #" .. group_id .. "."
  end
  return "group", group_id
end

local function require_task(ctx, item_id, need_edit)
  if not item_id then
    return nil, false, "Give the task id."
  end
  local item = ctx.store.get("items", item_id)
  if not item or item.kind ~= "task" then
    return nil, false, "There is no task #" .. tostring(item_id) .. "."
  end
  if item.owner_kind == "group" then
    local grp = ctx.store.get("groups", item.owner_id)
    if not grp or not is_member(grp, ctx.author_id) then
      return nil, false, "That task belongs to a group you are not in."
    end
    return item, true
  end
  if item.owner_id == ctx.author_id then
    return item, true
  end
  for _, share in ipairs(item.shares or {}) do
    if share.user == ctx.author_id then
      if need_edit and not share.can_edit then
        return nil, false, "That task is shared with you as view-only."
      end
      return item, share.can_edit and true or false
    end
  end
  return nil, false, "You do not have access to that task."
end

local function first_mention(ctx)
  for _, m in ipairs(ctx.mentions) do
    if not m.bot then return m end
  end
  return nil
end

local function dest_label(ctx, kind, id)
  if kind == "group" then return "group #" .. id end
  if id == ctx.author_id then return "your personal space" end
  return ctx.user_name(id)
end

local function parse_dest(ctx, rest)
  local first = (rest or ""):match("^%s*(%S+)")
  if first then
    local low = first:lower()
    if low == "me" or low == "self" or low == "mine" then
      return "user", ctx.author_id
    end
    local gid = first:match("^#(%d+)$")
    if gid then
      local kind, oid, err = scope_for(ctx, gid)
      if err then return nil, nil, err end
      return kind, oid
    end
  end
  local mention = first_mention(ctx)
  if mention then return "user", mention.id end
  return nil, nil, "Destination must be `me`, an @mention, or `#<groupid>`."
end

local function rank(item)
  return item.done and 1 or 0
end

local function task_order(a, b)
  if rank(a) ~= rank(b) then return rank(a) < rank(b) end
  local ad = a.due_at or math.huge
  local bd = b.due_at or math.huge
  if ad ~= bd then return ad < bd end
  return (tonumber(a.id) or 0) < (tonumber(b.id) or 0)
end

local function task_line(it)
  local box = it.done and "[x]" or "[ ]"
  local due = it.due_at and ("  due " .. arch.fmt_time(it.due_at)) or ""
  return "`#" .. it.id .. "` " .. box .. " " .. arch.clip(it.title, 70) .. due
end

local function list_pages(title, items, empty)
  if #items == 0 then
    return { { title = title, description = empty, color = arch.colors.neutral } }
  end
  table.sort(items, task_order)
  local pages = {}
  for start = 1, #items, PAGE do
    local lines = {}
    for i = start, math.min(start + PAGE - 1, #items) do
      lines[#lines + 1] = task_line(items[i])
    end
    pages[#pages + 1] = {
      title = title, color = arch.colors.teal,
      description = table.concat(lines, "\n"),
      footer = #items .. " task(s)",
    }
  end
  return pages
end

-- ── command handlers ─────────────────────────────────────────────────────────
local function do_list(ctx)
  local s = arch.sigils(ctx.args)
  local owner_kind, owner_id, err = scope_for(ctx, s.group)
  if err then ctx.error(err) return end
  local list_name = s.list
  if not list_name and s.text ~= "" then
    local first = s.text:match("^(%S+)")
    if first then list_name = first:lower() end
  end
  local filter = { kind = "task", owner_kind = owner_kind, owner_id = owner_id }
  if list_name then filter.list_name = list_name end
  local items = ctx.store.query("items", filter)
  local where = owner_kind == "user" and "Your tasks"
    or ("Group #" .. owner_id .. " tasks")
  if list_name then where = where .. " -- ~" .. list_name end
  ctx.deliver(
    list_pages(where, items,
      "No tasks here yet. Add one with `" .. ctx.prefix .. "task add`."),
    { private = owner_kind ~= "group" })
end

local function do_add(ctx)
  local s = arch.sigils(ctx.args)
  if s.text == "" then ctx.error("Give the task text.") return end
  local owner_kind, owner_id, err = scope_for(ctx, s.group)
  if err then ctx.error(err) return end
  local list_name = s.list or "general"
  local id = ctx.store.put("items", {
    kind = "task", owner_kind = owner_kind, owner_id = owner_id,
    title = s.text:sub(1, 300), body = "", list_name = list_name,
    done = false, shares = {},
    created_by = ctx.author_id, created_at = arch.now(),
  })
  ctx.deliver({
    title = "Task added", color = arch.colors.success,
    description = "Saved as task `#" .. id .. "` in list `~" .. list_name
      .. "`. Set a reminder with `" .. ctx.prefix .. "task remind " .. id
      .. " <when>`.",
  }, { private = owner_kind ~= "group" })
end

local function do_lists(ctx)
  local s = arch.sigils(ctx.args)
  local owner_kind, owner_id, err = scope_for(ctx, s.group)
  if err then ctx.error(err) return end
  local items = ctx.store.query("items", {
    kind = "task", owner_kind = owner_kind, owner_id = owner_id,
  })
  local buckets = {}
  for _, it in ipairs(items) do
    local name = it.list_name or "general"
    buckets[name] = buckets[name] or { open = 0, total = 0 }
    buckets[name].total = buckets[name].total + 1
    if not it.done then buckets[name].open = buckets[name].open + 1 end
  end
  local names = {}
  for name in pairs(buckets) do names[#names + 1] = name end
  table.sort(names)
  local lines = {}
  for _, name in ipairs(names) do
    lines[#lines + 1] = "`~" .. name .. "` -- " .. buckets[name].open
      .. " open / " .. buckets[name].total .. " total"
  end
  local where = owner_kind == "user" and "Your task lists"
    or ("Group #" .. owner_id .. " task lists")
  ctx.deliver({
    title = where, color = arch.colors.teal,
    description = #lines > 0 and table.concat(lines, "\n")
      or "No task lists yet.",
  }, { private = owner_kind ~= "group" })
end

local function set_done(ctx, value)
  local id = ctx.args:match("^%s*(%S+)")
  local item, _, err = require_task(ctx, id, true)
  if err then ctx.error(err) return end
  item.done = value
  ctx.store.update("items", item.id, item)
  ctx.deliver({
    title = value and "Task done" or "Task reopened",
    color = arch.colors.success,
    description = "Task `#" .. item.id .. "` "
      .. (value and "is marked done." or "is open again."),
  }, { private = item.owner_kind ~= "group" })
end

local function do_due(ctx)
  local id, when = ctx.args:match("^%s*(%S+)%s*(.*)$")
  local item, _, err = require_task(ctx, id, true)
  if err then ctx.error(err) return end
  when = trim(when)
  local low = when:lower()
  if when == "" or low == "clear" or low == "none" or low == "off" then
    item.due_at = nil
    ctx.store.update("items", item.id, item)
    ctx.deliver({
      title = "Due date cleared", color = arch.colors.success,
      description = "Cleared the due date on task `#" .. item.id .. "`.",
    }, { private = item.owner_kind ~= "group" })
    return
  end
  local epoch = arch.parse_time(when)
  if not epoch then ctx.error(TIME_HINT) return end
  item.due_at = epoch
  ctx.store.update("items", item.id, item)
  ctx.deliver({
    title = "Due date set", color = arch.colors.success,
    description = "Task `#" .. item.id .. "` is due " .. arch.fmt_time(epoch)
      .. ".",
  }, { private = item.owner_kind ~= "group" })
end

local function do_remind(ctx)
  local id, when = ctx.args:match("^%s*(%S+)%s*(.*)$")
  local item, _, err = require_task(ctx, id, true)
  if err then ctx.error(err) return end
  when = trim(when)
  local low = when:lower()
  if when == "" or low == "clear" or low == "none" or low == "off" then
    item.remind_at = nil
    item.reminded = false
    ctx.store.update("items", item.id, item)
    ctx.deliver({
      title = "Reminder cleared", color = arch.colors.success,
      description = "Cleared the reminder on task `#" .. item.id .. "`.",
    }, { private = item.owner_kind ~= "group" })
    return
  end
  local epoch = arch.parse_time(when)
  if not epoch then ctx.error(TIME_HINT) return end
  item.remind_at = epoch
  item.reminded = false
  ctx.store.update("items", item.id, item)
  ctx.deliver({
    title = "Reminder set", color = arch.colors.success,
    description = "I will remind about task `#" .. item.id .. "` at "
      .. arch.fmt_time(epoch) .. ".",
  }, { private = item.owner_kind ~= "group" })
end

local function do_edit(ctx)
  local id, text = ctx.args:match("^%s*(%S+)%s*(.*)$")
  local item, _, err = require_task(ctx, id, true)
  if err then ctx.error(err) return end
  text = trim(text)
  if text == "" then ctx.error("Give the new text after the id.") return end
  item.title = text:sub(1, 300)
  ctx.store.update("items", item.id, item)
  ctx.deliver({
    title = "Updated", color = arch.colors.success,
    description = "Edited task `#" .. item.id .. "`.",
  }, { private = item.owner_kind ~= "group" })
end

local function do_del(ctx)
  local id = ctx.args:match("^%s*(%S+)")
  local item, _, err = require_task(ctx, id, true)
  if err then ctx.error(err) return end
  ctx.store.delete("items", item.id)
  ctx.deliver({
    title = "Deleted", color = arch.colors.success,
    description = "Removed task `#" .. item.id .. "`.",
  }, { private = item.owner_kind ~= "group" })
end

local function do_share(ctx)
  local id = ctx.args:match("^%s*(%S+)")
  local item, _, err = require_task(ctx, id, false)
  if err then ctx.error(err) return end
  if item.owner_kind ~= "user" or item.owner_id ~= ctx.author_id then
    ctx.error("You can only share your own personal tasks.")
    return
  end
  local target = first_mention(ctx)
  if not target then ctx.error("Mention the user to share it with.") return end
  if target.id == ctx.author_id then
    ctx.error("You already own that task.")
    return
  end
  local can_edit = ctx.args:lower():find("edit", 1, true) ~= nil
  item.shares = item.shares or {}
  local found = false
  for _, share in ipairs(item.shares) do
    if share.user == target.id then
      share.can_edit = can_edit
      found = true
    end
  end
  if not found then
    item.shares[#item.shares + 1] = { user = target.id, can_edit = can_edit }
  end
  ctx.store.update("items", item.id, item)
  local access = can_edit and "view and edit" or "view"
  arch.dm(target.id, {
    title = "A task was shared with you", color = arch.colors.info,
    description = ctx.author_name .. " shared task `#" .. item.id .. "` ("
      .. arch.clip(item.title, 120) .. ") with you (" .. access .. ").",
  })
  ctx.deliver({
    title = "Shared", color = arch.colors.success,
    description = "Task `#" .. item.id .. "` is now shared with "
      .. target.name .. " (" .. access .. ").",
  }, { private = true })
end

local function do_unshare(ctx)
  local id = ctx.args:match("^%s*(%S+)")
  local item, _, err = require_task(ctx, id, false)
  if err then ctx.error(err) return end
  if item.owner_kind ~= "user" or item.owner_id ~= ctx.author_id then
    ctx.error("You can only unshare your own tasks.")
    return
  end
  local target = first_mention(ctx)
  if not target then
    ctx.error("Mention the user to stop sharing with.")
    return
  end
  local kept, removed = {}, false
  for _, share in ipairs(item.shares or {}) do
    if share.user == target.id then
      removed = true
    else
      kept[#kept + 1] = share
    end
  end
  if not removed then
    ctx.error(target.name .. " did not have access to that task.")
    return
  end
  item.shares = kept
  ctx.store.update("items", item.id, item)
  ctx.deliver({
    title = "Unshared", color = arch.colors.success,
    description = target.name .. " can no longer see task `#" .. item.id .. "`.",
  }, { private = true })
end

local function do_copy(ctx)
  local id, rest = ctx.args:match("^%s*(%S+)%s*(.*)$")
  local item, _, err = require_task(ctx, id, false)
  if err then ctx.error(err) return end
  local kind, oid, derr = parse_dest(ctx, rest)
  if derr then ctx.error(derr) return end
  local new_id = ctx.store.put("items", {
    kind = "task", owner_kind = kind, owner_id = oid,
    title = item.title, body = item.body or "",
    list_name = item.list_name or "general", done = item.done and true or false,
    due_at = item.due_at, shares = {},
    created_by = ctx.author_id, created_at = arch.now(),
  })
  ctx.deliver({
    title = "Copied", color = arch.colors.success,
    description = "Task copied to " .. dest_label(ctx, kind, oid)
      .. " as `#" .. new_id .. "`.",
  }, { private = kind ~= "group" })
end

local function do_move(ctx)
  local id, rest = ctx.args:match("^%s*(%S+)%s*(.*)$")
  local item, _, err = require_task(ctx, id, true)
  if err then ctx.error(err) return end
  local kind, oid, derr = parse_dest(ctx, rest)
  if derr then ctx.error(derr) return end
  if kind == item.owner_kind and oid == item.owner_id then
    ctx.error("That task is already there.")
    return
  end
  item.owner_kind, item.owner_id, item.shares = kind, oid, {}
  ctx.store.update("items", item.id, item)
  ctx.deliver({
    title = "Moved", color = arch.colors.success,
    description = "Task `#" .. item.id .. "` moved to "
      .. dest_label(ctx, kind, oid) .. ".",
  }, { private = kind ~= "group" })
end

-- ── reminder loop ────────────────────────────────────────────────────────────
local function fire_reminders()
  local now = arch.now()
  for _, item in ipairs(arch.store.query("items", { kind = "task" })) do
    if item.remind_at and not item.reminded and item.remind_at <= now then
      local recipients
      if item.owner_kind == "group" then
        local grp = arch.store.get("groups", item.owner_id)
        recipients = grp and (grp.members or {}) or {}
      else
        recipients = { item.owner_id }
      end
      local fields = {}
      if item.due_at then
        fields[#fields + 1] = { name = "Due",
                                value = arch.fmt_time(item.due_at), inline = true }
      end
      local note = {
        title = "Task reminder", color = arch.colors.gold,
        description = "**" .. arch.clip(item.title, 240) .. "**",
        fields = fields, footer = "task #" .. item.id,
      }
      for _, uid in ipairs(recipients) do
        arch.dm(uid, note)
      end
      item.reminded = true
      arch.store.update("items", item.id, item)
    end
  end
end

-- ── command tree ─────────────────────────────────────────────────────────────
M.commands = {
  {
    name = "task", aliases = { "tasks", "todo" },
    summary = "Tasks and to-do lists.",
    run = do_list,
    subcommands = {
      { name = "add", aliases = { "new", "create" },
        usage = "add [#group] [~list] <text>",
        summary = "Add a task to a list.", run = do_add },
      { name = "list", aliases = { "ls", "all" }, usage = "list [#group] [~list]",
        summary = "List your tasks, filtered by list.", run = do_list },
      { name = "lists", usage = "lists [#group]",
        summary = "Show every task list and its open count.", run = do_lists },
      { name = "done", aliases = { "complete", "check" }, usage = "done <id>",
        summary = "Mark a task done.",
        run = function(ctx) set_done(ctx, true) end },
      { name = "undone", aliases = { "uncheck", "reopen" }, usage = "undone <id>",
        summary = "Reopen a completed task.",
        run = function(ctx) set_done(ctx, false) end },
      { name = "due", usage = "due <id> <when|clear>",
        summary = "Set or clear a task's due date.", run = do_due },
      { name = "remind", usage = "remind <id> <when|clear>",
        summary = "Set or clear a task reminder.", run = do_remind },
      { name = "edit", usage = "edit <id> <text>",
        summary = "Replace a task's text.", run = do_edit },
      { name = "del", aliases = { "delete", "rm", "remove" }, usage = "del <id>",
        summary = "Delete a task.", run = do_del },
      { name = "share", usage = "share <id> @user [edit]",
        summary = "Share a personal task with a user.", run = do_share },
      { name = "unshare", usage = "unshare <id> @user",
        summary = "Stop sharing a task with a user.", run = do_unshare },
      { name = "copy", usage = "copy <id> <me|@user|#group>",
        summary = "Copy a task somewhere else.", run = do_copy },
      { name = "move", usage = "move <id> <me|@user|#group>",
        summary = "Move a task somewhere else.", run = do_move },
    },
  },
}

M.loops = {
  { name = "reminders", interval = 60, run = fire_reminders },
}

return M
