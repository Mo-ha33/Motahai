import { useRef, useState } from 'react';
import type { Dictionary } from '../i18n';

type CopyState = 'idle' | 'copied' | 'failed';

/** A read-only value with a copy button. Falls back to selecting the text when the clipboard API is unavailable. */
export function CopyField({ label, value, t }: { label: string; value: string; t: Dictionary['onboarding'] }) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [state, setState] = useState<CopyState>('idle');

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(value);
      setState('copied');
    } catch {
      inputRef.current?.focus();
      inputRef.current?.select();
      setState('failed');
    }
  };

  return (
    <div className="copy-field">
      <span className="filter-label">{label}</span>
      <div className="copy-row">
        <input
          ref={inputRef}
          className="filter-input copy-input"
          readOnly
          dir="ltr"
          value={value}
          aria-label={label}
          autoComplete="off"
          spellCheck={false}
          onFocus={(event) => event.currentTarget.select()}
        />
        <button type="button" className="button copy-button" onClick={copy} aria-label={`${t.copy}: ${label}`}>
          {t.copy}
        </button>
      </div>
      <div role="status" className={state === 'failed' ? 'filter-error' : 'filter-help'}>
        {state === 'copied' ? t.copied : state === 'failed' ? t.copyFailed : ''}
      </div>
    </div>
  );
}
