/** A CJK system font can give curly apostrophes a full cell even with lang="en".
 * Style Latin prose, including before language detection, without changing its text
 * or narrowing punctuation in CJK and mixed-script paragraphs.
 */
export function bookTextFont(text: string | null | undefined) {
  return text && /\p{Script=Latin}/u.test(text) &&
    !/[^\p{Script=Latin}\p{Script=Common}\p{Script=Inherited}]/u.test(text)
    ? "font-book-latin" : undefined;
}
