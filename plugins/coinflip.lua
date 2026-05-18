-- coinflip.lua -- example Lua plugin: a single fun.coinflip tool.
--
-- Demonstrates the plugin contract. Delete this file if you do not want
-- the tool, or use it as a template for your own.

math.randomseed(os.time())

return {
  {
    name = "fun.coinflip",
    description = "Flip a fair coin. Use when a user asks for a coin flip "
      .. "or a random heads/tails decision.",
    parameters = {
      type = "object",
      properties = {},
    },
    handler = function(args)
      if math.random(2) == 1 then
        return { result = "heads" }
      else
        return { result = "tails" }
      end
    end,
  },
}
