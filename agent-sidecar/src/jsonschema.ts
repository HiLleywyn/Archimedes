/**
 * Convert the OpenAI-style JSON Schema that Archimedes tools declare into the
 * Zod schemas the Agent SDK's tool() helper requires.
 *
 * The bot's tool registry speaks JSON Schema; the SDK speaks Zod. This is the
 * one place that translation happens. The conversion stays faithful so the
 * schema the model ultimately sees (the SDK re-derives JSON Schema from Zod)
 * carries the same field names, types, descriptions and enums.
 */
import { z } from 'zod';

type JsonSchema = Record<string, unknown>;

function isObject(value: unknown): value is JsonSchema {
  return typeof value === 'object' && value !== null;
}

function convert(schema: unknown): z.ZodType {
  if (!isObject(schema)) {
    return z.any();
  }

  if (Array.isArray(schema.enum) && schema.enum.length > 0) {
    const values = schema.enum as unknown[];
    if (values.every((value) => typeof value === 'string')) {
      return z.enum(values as [string, ...string[]]);
    }
    return z.any();
  }

  switch (schema.type) {
    case 'string':
      return z.string();
    case 'integer':
      return z.number().int();
    case 'number':
      return z.number();
    case 'boolean':
      return z.boolean();
    case 'array': {
      const items = schema.items;
      const itemSchema =
        isObject(items) && Object.keys(items).length > 0
          ? convert(items)
          : z.any();
      return z.array(itemSchema);
    }
    case 'object':
      return convertObject(schema);
    default:
      return schema.properties ? convertObject(schema) : z.any();
  }
}

function convertObject(schema: JsonSchema): z.ZodObject {
  const properties = isObject(schema.properties) ? schema.properties : {};
  const required = new Set(
    Array.isArray(schema.required) ? (schema.required as string[]) : [],
  );
  const shape: Record<string, z.ZodType> = {};
  for (const [key, raw] of Object.entries(properties)) {
    let field = convert(raw);
    const description = isObject(raw) ? raw.description : undefined;
    if (typeof description === 'string' && description) {
      field = field.describe(description);
    }
    if (!required.has(key)) {
      field = field.optional();
    }
    shape[key] = field;
  }
  return z.object(shape);
}

/**
 * Convert a JSON Schema into a Zod object schema suitable for tool().
 *
 * A non-object schema is wrapped as an empty object: the SDK's tool() helper
 * only accepts an object schema for tool parameters.
 */
export function toZodObject(schema: unknown): z.ZodObject {
  const converted = convert(schema);
  if (converted instanceof z.ZodObject) {
    return converted;
  }
  return z.object({});
}
