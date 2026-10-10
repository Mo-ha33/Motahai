import { useState, type FormEvent } from 'react';
import { dictionaries, type Lang } from '../i18n';
import { useSession, type SignInResult } from './SessionContext';

export function SignIn({ lang }: { lang: Lang }) {
  const t = dictionaries[lang].auth;
  const { notice, signInTenant, signInOperator } = useSession();
  const [key, setKey] = useState('');
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<Exclude<SignInResult, 'ok'> | null>(null);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setFailure(null);
    const result = await signInTenant(key);
    if (result !== 'ok') {
      setBusy(false);
      setFailure(result);
      // A rejected key is not kept around in the input.
      if (result === 'invalid') setKey('');
    }
  };

  const errorText =
    failure === 'invalid' ? t.invalidKey : failure === 'format' ? t.formatError : failure === 'error' ? t.networkError : null;

  return (
    <main className="signin">
      <section className="card signin-card" aria-labelledby="signin-title">
        <h1 id="signin-title" className="signin-title">
          {t.title}
        </h1>
        {notice === 'expired' ? (
          <p className="signin-note" role="status">
            {t.expired}
          </p>
        ) : null}
        <form onSubmit={submit} className="signin-form">
          <label className="filter-label" htmlFor="signin-key">
            {t.keyLabel}
          </label>
          <input
            id="signin-key"
            className="filter-input"
            type="password"
            autoComplete="off"
            autoCapitalize="off"
            spellCheck={false}
            dir="ltr"
            value={key}
            aria-invalid={errorText ? true : undefined}
            aria-describedby="signin-help"
            onChange={(event) => setKey(event.target.value)}
          />
          <div id="signin-help" className="filter-help">
            {t.keyHelp}
          </div>
          {errorText ? (
            <div className="filter-error" role="alert">
              {errorText}
            </div>
          ) : null}
          <button type="submit" className="button button-primary" disabled={busy || key.trim() === ''}>
            {busy ? t.submitting : t.submit}
          </button>
        </form>
        <hr className="signin-divider" />
        <button type="button" className="button" onClick={signInOperator}>
          {t.operatorMode}
        </button>
        <div className="filter-help">{t.operatorHint}</div>
      </section>
    </main>
  );
}
