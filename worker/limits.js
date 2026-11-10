// Counts of things one address did lately (convoys started; wrong owner keys): a Limits object for each
// address and kind (named by them), forgotten once its window has passed.
import { DurableObject } from "cloudflare:workers";

export class Limits extends DurableObject {
  async times(windowSeconds) {
    const since = Date.now() - windowSeconds * 1000;
    return ((await this.ctx.storage.get("times")) || []).filter((t) => t > since);
  }

  async note(times, windowSeconds) {
    const now = Date.now();
    times.push(now);
    await this.ctx.storage.put("times", times);
    await this.ctx.storage.setAlarm(now + windowSeconds * 1000); // forget the address once the window has passed
  }

  /** True while this address did it fewer than `most` times in the last `windowSeconds` (and counts this one). */
  async allow(most, windowSeconds) {
    const times = await this.times(windowSeconds);
    if (times.length >= most) return false;
    await this.note(times, windowSeconds);
    return true;
  }

  /** Whether this address has done it `most` times in the last `windowSeconds` (counts nothing). */
  async blocked(most, windowSeconds) {
    return (await this.times(windowSeconds)).length >= most;
  }

  /** Counts one, whatever the total. */
  async count(windowSeconds) {
    await this.note(await this.times(windowSeconds), windowSeconds);
  }

  async alarm() {
    await this.ctx.storage.deleteAll();
  }
}
