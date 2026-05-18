-- groups.lua -- the Groups plugin for Archimedes.
--
-- A group is a shared space: every member can see and edit the group's
-- notes, tasks and events, and group replies post in the channel. Groups
-- carry an owner, a member list and pending invitations, all stored as
-- documents in the shared `productivity` namespace.
--
-- Standalone plugin: registers the `.group` command group. The notes, tasks
-- and events plugins read this plugin's `groups` collection to resolve a
-- `#<groupid>` scope token, so the four plugins work as a suite.

local M = {}

M.manifest = {
  id = "groups",
  name = "Groups",
  version = "1.0.0",
  description = "Shared groups for notes, tasks and events.",
  author = "HiLleywyn",
  category = "Productivity",
  storage = "productivity",
}

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

local function first_mention(ctx)
  for _, m in ipairs(ctx.mentions) do
    if not m.bot then return m end
  end
  return nil
end

local function owned_group(ctx, group_id)
  if not group_id then
    return nil, "Give the group id."
  end
  local grp = ctx.store.get("groups", group_id)
  if not grp then
    return nil, "There is no group #" .. tostring(group_id) .. "."
  end
  if grp.owner_id ~= ctx.author_id then
    return nil, "Only the group owner can do that."
  end
  return grp
end

-- ── command handlers ─────────────────────────────────────────────────────────
local function do_list(ctx)
  local groups = ctx.store.query("groups", { members = { ctx.author_id } })
  if #groups == 0 then
    ctx.deliver({
      title = "Your groups", color = arch.colors.blurple,
      description = "You are not in any groups yet. Create one with `"
        .. ctx.prefix .. "group create <name>`.",
    }, { private = true })
    return
  end
  local lines = {}
  for _, g in ipairs(groups) do
    local role = g.owner_id == ctx.author_id and "owner" or "member"
    lines[#lines + 1] = "`#" .. g.id .. "` " .. g.name .. " -- " .. role
  end
  ctx.deliver({
    title = "Your groups", color = arch.colors.blurple,
    description = table.concat(lines, "\n"),
    footer = ctx.prefix .. "group show <id> for details   "
      .. ctx.prefix .. "group invites for pending invites",
  }, { private = true })
end

local function do_create(ctx)
  local name = trim(ctx.args)
  if name == "" then ctx.error("Give the group a name.") return end
  for _, g in ipairs(ctx.store.query("groups", { members = { ctx.author_id } })) do
    if g.name:lower() == name:lower() then
      ctx.error("You are already in a group called `" .. name .. "`.")
      return
    end
  end
  local id = ctx.store.put("groups", {
    guild_id = ctx.guild_id, name = name:sub(1, 100),
    owner_id = ctx.author_id, members = { ctx.author_id }, invites = {},
    created_at = arch.now(),
  })
  ctx.deliver({
    title = "Group created", color = arch.colors.success,
    description = "`" .. name .. "` is group `#" .. id .. "`. Invite members "
      .. "with `" .. ctx.prefix .. "group invite " .. id .. " @user`.",
  }, { private = true })
end

local function do_show(ctx)
  local gid = ctx.args:match("^%s*(%S+)")
  if not gid then ctx.error("Give the group id.") return end
  local grp = ctx.store.get("groups", gid)
  if not grp then ctx.error("There is no group #" .. gid .. ".") return end
  if not is_member(grp, ctx.author_id) then
    ctx.error("Only members can view that group.")
    return
  end
  local counts = { note = 0, task = 0, event = 0 }
  for _, it in ipairs(ctx.store.query("items",
      { owner_kind = "group", owner_id = gid })) do
    counts[it.kind] = (counts[it.kind] or 0) + 1
  end
  local member_lines = {}
  for _, uid in ipairs(grp.members or {}) do
    local tag = uid == grp.owner_id and " (owner)" or ""
    member_lines[#member_lines + 1] = "- " .. ctx.user_name(uid) .. tag
  end
  ctx.deliver({
    title = "Group " .. grp.name .. " (#" .. gid .. ")",
    color = arch.colors.blurple,
    fields = {
      { name = "Members", value = table.concat(member_lines, "\n") },
      { name = "Notes", value = tostring(counts.note or 0), inline = true },
      { name = "Tasks", value = tostring(counts.task or 0), inline = true },
      { name = "Events", value = tostring(counts.event or 0), inline = true },
    },
    footer = ctx.prefix .. "note list #" .. gid .. "   "
      .. ctx.prefix .. "task list #" .. gid .. "   "
      .. ctx.prefix .. "event list #" .. gid,
  }, { private = false })
end

local function do_invite(ctx)
  local gid = ctx.args:match("^%s*(%S+)")
  local grp, err = owned_group(ctx, gid)
  if err then ctx.error(err) return end
  local target = first_mention(ctx)
  if not target then ctx.error("Mention the user to invite.") return end
  if target.bot then ctx.error("You cannot invite a bot.") return end
  if is_member(grp, target.id) then
    ctx.error(target.name .. " is already in that group.")
    return
  end
  grp.invites = grp.invites or {}
  local found = false
  for _, inv in ipairs(grp.invites) do
    if inv.invitee == target.id then
      inv.inviter = ctx.author_id
      found = true
    end
  end
  if not found then
    grp.invites[#grp.invites + 1] =
      { invitee = target.id, inviter = ctx.author_id }
  end
  ctx.store.update("groups", grp.id, grp)
  arch.dm(target.id, {
    title = "Group invitation", color = arch.colors.info,
    description = ctx.author_name .. " invited you to the group `" .. grp.name
      .. "` (#" .. grp.id .. "). Accept with `" .. ctx.prefix .. "group join "
      .. grp.id .. "` or decline with `" .. ctx.prefix .. "group decline "
      .. grp.id .. "`.",
  })
  ctx.deliver({
    title = "Invite sent", color = arch.colors.success,
    description = "Invited " .. target.name .. " to `" .. grp.name .. "`.",
  }, { private = true })
end

local function do_invites(ctx)
  local groups = ctx.store.query("groups",
    { invites = { { invitee = ctx.author_id } } })
  if #groups == 0 then
    ctx.deliver({
      title = "Your group invites", color = arch.colors.blurple,
      description = "You have no pending group invitations.",
    }, { private = true })
    return
  end
  local lines = {}
  for _, g in ipairs(groups) do
    local inviter = "someone"
    for _, inv in ipairs(g.invites or {}) do
      if inv.invitee == ctx.author_id then
        inviter = ctx.user_name(inv.inviter)
      end
    end
    lines[#lines + 1] = "`#" .. g.id .. "` " .. g.name .. " -- from " .. inviter
  end
  ctx.deliver({
    title = "Your group invites", color = arch.colors.blurple,
    description = table.concat(lines, "\n"),
    footer = ctx.prefix .. "group join <id> to accept",
  }, { private = true })
end

local function do_join(ctx)
  local gid = ctx.args:match("^%s*(%S+)")
  if not gid then ctx.error("Give the group id.") return end
  local grp = ctx.store.get("groups", gid)
  if not grp then ctx.error("There is no group #" .. gid .. ".") return end
  local kept, invited = {}, false
  for _, inv in ipairs(grp.invites or {}) do
    if inv.invitee == ctx.author_id then
      invited = true
    else
      kept[#kept + 1] = inv
    end
  end
  if not invited then
    ctx.error("You have no invitation to group #" .. gid .. ".")
    return
  end
  grp.invites = kept
  grp.members = grp.members or {}
  if not is_member(grp, ctx.author_id) then
    grp.members[#grp.members + 1] = ctx.author_id
  end
  ctx.store.update("groups", grp.id, grp)
  ctx.deliver({
    title = "Joined", color = arch.colors.success,
    description = "You are now a member of `" .. grp.name .. "`.",
  }, { private = true })
end

local function do_decline(ctx)
  local gid = ctx.args:match("^%s*(%S+)")
  if not gid then ctx.error("Give the group id.") return end
  local grp = ctx.store.get("groups", gid)
  if not grp then ctx.error("There is no group #" .. gid .. ".") return end
  local kept, found = {}, false
  for _, inv in ipairs(grp.invites or {}) do
    if inv.invitee == ctx.author_id then
      found = true
    else
      kept[#kept + 1] = inv
    end
  end
  if not found then
    ctx.error("You have no invitation to group #" .. gid .. ".")
    return
  end
  grp.invites = kept
  ctx.store.update("groups", grp.id, grp)
  ctx.deliver({
    title = "Declined", color = arch.colors.success,
    description = "Declined the invite to group #" .. gid .. ".",
  }, { private = true })
end

local function remove_member(grp, user_id)
  local kept, removed = {}, false
  for _, uid in ipairs(grp.members or {}) do
    if uid == user_id then
      removed = true
    else
      kept[#kept + 1] = uid
    end
  end
  grp.members = kept
  return removed
end

local function do_leave(ctx)
  local gid = ctx.args:match("^%s*(%S+)")
  if not gid then ctx.error("Give the group id.") return end
  local grp = ctx.store.get("groups", gid)
  if not grp then ctx.error("There is no group #" .. gid .. ".") return end
  if not is_member(grp, ctx.author_id) then
    ctx.error("You are not in that group.")
    return
  end
  if grp.owner_id == ctx.author_id then
    ctx.error("You own that group. Transfer it with `" .. ctx.prefix
      .. "group transfer " .. gid .. " @user` or delete it with `"
      .. ctx.prefix .. "group delete " .. gid .. "`.")
    return
  end
  remove_member(grp, ctx.author_id)
  ctx.store.update("groups", grp.id, grp)
  ctx.deliver({
    title = "Left group", color = arch.colors.success,
    description = "You left `" .. grp.name .. "`.",
  }, { private = true })
end

local function do_kick(ctx)
  local gid = ctx.args:match("^%s*(%S+)")
  local grp, err = owned_group(ctx, gid)
  if err then ctx.error(err) return end
  local target = first_mention(ctx)
  if not target then ctx.error("Mention the member to remove.") return end
  if target.id == ctx.author_id then
    ctx.error("You own the group. Use transfer or delete instead.")
    return
  end
  if not remove_member(grp, target.id) then
    ctx.error(target.name .. " is not in that group.")
    return
  end
  ctx.store.update("groups", grp.id, grp)
  ctx.deliver({
    title = "Member removed", color = arch.colors.success,
    description = "Removed " .. target.name .. " from `" .. grp.name .. "`.",
  }, { private = true })
end

local function do_rename(ctx)
  local gid, name = ctx.args:match("^%s*(%S+)%s*(.*)$")
  local grp, err = owned_group(ctx, gid)
  if err then ctx.error(err) return end
  name = trim(name)
  if name == "" then ctx.error("Give the new group name.") return end
  local old = grp.name
  grp.name = name:sub(1, 100)
  ctx.store.update("groups", grp.id, grp)
  ctx.deliver({
    title = "Group renamed", color = arch.colors.success,
    description = "`" .. old .. "` is now `" .. grp.name .. "`.",
  }, { private = true })
end

local function do_transfer(ctx)
  local gid = ctx.args:match("^%s*(%S+)")
  local grp, err = owned_group(ctx, gid)
  if err then ctx.error(err) return end
  local target = first_mention(ctx)
  if not target then
    ctx.error("Mention the member to hand the group to.")
    return
  end
  if not is_member(grp, target.id) then
    ctx.error(target.name .. " must be a group member first.")
    return
  end
  grp.owner_id = target.id
  ctx.store.update("groups", grp.id, grp)
  ctx.deliver({
    title = "Ownership transferred", color = arch.colors.success,
    description = target.name .. " now owns `" .. grp.name .. "`.",
  }, { private = true })
end

local function do_delete(ctx)
  local gid = ctx.args:match("^%s*(%S+)")
  local grp, err = owned_group(ctx, gid)
  if err then ctx.error(err) return end
  if not ctx.confirm("Delete group `" .. grp.name .. "` and all of its notes, "
      .. "tasks and events? This cannot be undone.") then
    ctx.error("Group deletion cancelled.")
    return
  end
  local items = ctx.store.query("items",
    { owner_kind = "group", owner_id = gid })
  for _, it in ipairs(items) do
    ctx.store.delete("items", it.id)
  end
  ctx.store.delete("groups", gid)
  ctx.deliver({
    title = "Group deleted", color = arch.colors.success,
    description = "`" .. grp.name .. "` and its " .. #items
      .. " item(s) are gone.",
  }, { private = true })
end

local function do_duplicate(ctx)
  local gid = ctx.args:match("^%s*(%S+)")
  if not gid then ctx.error("Give the group id.") return end
  local grp = ctx.store.get("groups", gid)
  if not grp then ctx.error("There is no group #" .. gid .. ".") return end
  if not is_member(grp, ctx.author_id) then
    ctx.error("Only members can duplicate that group.")
    return
  end
  local new_id = ctx.store.put("groups", {
    guild_id = grp.guild_id, name = arch.clip(grp.name .. " (copy)", 100),
    owner_id = ctx.author_id, members = { ctx.author_id }, invites = {},
    created_at = arch.now(),
  })
  local items = ctx.store.query("items",
    { owner_kind = "group", owner_id = gid })
  for _, it in ipairs(items) do
    ctx.store.put("items", {
      kind = it.kind, owner_kind = "group", owner_id = new_id,
      title = it.title, body = it.body or "",
      list_name = it.list_name or "general",
      done = it.done and true or false, due_at = it.due_at,
      remind_at = it.remind_at, reminded = false, shares = {},
      created_by = ctx.author_id, created_at = arch.now(),
    })
  end
  ctx.deliver({
    title = "Group duplicated", color = arch.colors.success,
    description = "Copied " .. #items .. " item(s) into new group `#"
      .. new_id .. "` (" .. grp.name .. " (copy)). You are the owner.",
  }, { private = true })
end

-- ── command tree ─────────────────────────────────────────────────────────────
M.commands = {
  {
    name = "group", aliases = { "groups" },
    summary = "Shared groups for notes, tasks and events.",
    run = do_list,
    subcommands = {
      { name = "create", aliases = { "new" }, usage = "create <name>",
        summary = "Create a group in this server.", guild_only = true,
        run = do_create },
      { name = "list", aliases = { "ls", "mine" }, usage = "list",
        summary = "List the groups you belong to.", run = do_list },
      { name = "show", aliases = { "view", "info" }, usage = "show <id>",
        summary = "Show a group's members and item counts.", run = do_show },
      { name = "invite", usage = "invite <id> @user",
        summary = "Invite a user to a group you own.", run = do_invite },
      { name = "invites", aliases = { "pending" }, usage = "invites",
        summary = "List group invitations waiting for you.", run = do_invites },
      { name = "join", aliases = { "accept" }, usage = "join <id>",
        summary = "Accept a pending group invitation.", run = do_join },
      { name = "decline", aliases = { "reject" }, usage = "decline <id>",
        summary = "Decline a pending group invitation.", run = do_decline },
      { name = "leave", usage = "leave <id>",
        summary = "Leave a group you belong to.", run = do_leave },
      { name = "kick", aliases = { "remove" }, usage = "kick <id> @user",
        summary = "Remove a member from a group you own.", run = do_kick },
      { name = "rename", usage = "rename <id> <name>",
        summary = "Rename a group you own.", run = do_rename },
      { name = "transfer", usage = "transfer <id> @user",
        summary = "Hand group ownership to another member.",
        run = do_transfer },
      { name = "delete", aliases = { "disband" }, usage = "delete <id>",
        summary = "Delete a group you own and everything in it.",
        run = do_delete },
      { name = "duplicate", aliases = { "clone" }, usage = "duplicate <id>",
        summary = "Clone a group's items into a fresh group.",
        run = do_duplicate },
    },
  },
}

return M
