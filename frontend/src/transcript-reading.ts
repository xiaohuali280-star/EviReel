import type {Transcript} from './types';

export type ReadingMode = 'paragraphs' | 'segments';
export const modeKey = 'evireel:transcript-reading-mode';
export function readMode(storage: Pick<Storage, 'getItem'> = localStorage): ReadingMode {
  try {return storage.getItem(modeKey) === 'segments' ? 'segments' : 'paragraphs'} catch {return 'paragraphs'}
}
export function targetDuration(duration: number) {
  return Number.isFinite(duration) && duration > 0 ? Math.max(12, Math.min(45, Math.round(22 * Math.sqrt(duration / 480)))) : 22;
}
export function sourceId(segment: Transcript) {return `${segment.transcript_version}:${segment.segment_id}`}
export interface Paragraph {id: string; start_t: number; end_t: number; text: string; sources: Transcript[]}
const sentenceEnd = /[。！？.!?][”’"'）)】\]]*\s*$/u;
function join(left: string, right: string) {
  if (!left || !right || /\s$/u.test(left) || /^\s/u.test(right)) return left + right;
  return left + (/[\p{Script=Han}。！？、，；：]$/u.test(left) || /^[\p{Script=Han}。！？、，；：]/u.test(right) ? '' : ' ') + right;
}

/** Append-only reading groups. Original segments remain the indivisible timing unit. */
export class ParagraphBuilder {
  private finished: Paragraph[] = [];
  private tail: Paragraph | null = null;
  private chars = 0;
  readonly target: number;
  constructor(duration: number) {this.target = targetDuration(duration)}
  private flush() {if (this.tail) this.finished.push(this.tail); this.tail = null; this.chars = 0}
  append(segments: Transcript[]) {
    for (const segment of segments) {
      const length = Array.from(segment.text).length;
      const tail = this.tail;
      if (tail && (segment.start_t - tail.end_t >= 2 ||
          Math.max(tail.end_t, segment.end_t) - Math.min(tail.start_t, segment.start_t) > Math.min(2 * this.target, 90) ||
          this.chars + length + (join(tail.text, segment.text).length > tail.text.length + segment.text.length ? 1 : 0) > 320)) this.flush();
      if (!this.tail) this.tail = {id: sourceId(segment), start_t: segment.start_t, end_t: segment.end_t, text: segment.text, sources: [segment]};
      else this.tail = {...this.tail, start_t: Math.min(this.tail.start_t, segment.start_t), end_t: Math.max(this.tail.end_t, segment.end_t),
        text: join(this.tail.text, segment.text), sources: [...this.tail.sources, segment]};
      this.chars = Array.from(this.tail.text).length;
      if ((this.tail.end_t - this.tail.start_t >= this.target || this.chars >= 180) && sentenceEnd.test(segment.text)) this.flush();
    }
    return this.snapshot();
  }
  snapshot() {return this.tail ? [...this.finished, this.tail] : [...this.finished]}
}
export function groupTranscript(segments: Transcript[], duration: number) {return new ParagraphBuilder(duration).append(segments)}
export function isSpeaking(sources: Transcript[], now: number) {return sources.some(s => now >= s.start_t && now < s.end_t)}
