// Without inline language metadata, keep the font's punctuation conventions
// for East Asian and mixed CJK paragraphs rather than guessing who owns a quote.
const eastAsianText = /[\p{scx=Han}\p{scx=Hiragana}\p{scx=Katakana}\p{scx=Hangul}\p{scx=Bopomofo}]/u;

export function bookTextFont(text: string | null | undefined) {
  return text && /\p{Letter}/u.test(text) && !eastAsianText.test(text) ? "font-book-quotes" : undefined;
}
