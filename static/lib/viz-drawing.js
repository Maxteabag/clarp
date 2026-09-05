// A pure drawing language, not JavaScript. No eval, property access, loops,
// functions, URLs or host capabilities. The worker also has a wall-clock fuse.
export function evaluateDrawing(program, state = {}, now = () => performance.now()) {
  if (!Array.isArray(program) || program.length > 64) throw Error('program limit');
  const started = now();
  let fuel = 512;
  function value(expr, depth = 0) {
    if (--fuel < 0 || depth > 8 || now() - started > 4) throw Error('drawing budget');
    let n;
    if (typeof expr === 'number') n = expr;
    else if (typeof expr === 'string' && ['events', 'weight', 'hot'].includes(expr)) n = state[expr] ?? 0;
    else if (Array.isArray(expr) && expr.length === 3) {
      const a = value(expr[1], depth + 1), b = value(expr[2], depth + 1);
      switch (expr[0]) {
        case 'add': n = a + b; break;
        case 'mul': n = a * b; break;
        case 'min': n = Math.min(a, b); break;
        case 'max': n = Math.max(a, b); break;
        default: throw Error('unknown expression');
      }
    } else throw Error('invalid expression');
    if (!Number.isFinite(n)) throw Error('non-finite coordinate');
    return n;
  }
  return program.map(command => {
    const arity = {circle:3, rect:4, line:4}[command?.op];
    if (!arity || !Array.isArray(command.args) || command.args.length !== arity) throw Error('invalid primitive');
    const args = command.args.map(x => Math.max(-4, Math.min(4, value(x))));
    if (command.op === 'circle' && args[2] < 0) throw Error('negative radius');
    return {op:command.op, args};
  });
}

export function seedPosition(id, agent) {
  let hash = 2166136261;
  for (const char of id) hash = Math.imul(hash ^ char.charCodeAt(0), 16777619) >>> 0;
  const angle = hash / 4294967296 * Math.PI * 2;
  const radius = (agent ? 90 : 240) + hash % 47;
  return {x: Math.cos(angle) * radius, y: Math.sin(angle) * radius};
}
