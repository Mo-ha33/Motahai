import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import App from './App';

/** Past the sign-in screen, in operator mode (the pre-sign-in behaviour). */
function renderOperator() {
  const view = render(<App />);
  // The operator choice is kept in sessionStorage, so a re-render in the same test may already be past sign-in.
  const button = screen.queryByRole('button', { name: /وضع المشغّل/ });
  if (button) fireEvent.click(button);
  return view;
}

describe('App shell', () => {
  beforeEach(() => {
    window.localStorage.clear();
    window.sessionStorage.clear();
    window.location.hash = '';
    document.documentElement.lang = '';
    document.documentElement.dir = '';
    document.documentElement.removeAttribute('data-theme');
  });

  afterEach(() => {
    cleanup();
  });

  it('renders Arabic by default with an RTL document', () => {
    renderOperator();

    expect(document.documentElement.dir).toBe('rtl');
    expect(document.documentElement.lang).toBe('ar');
    expect(screen.getByRole('button', { name: 'English' })).toBeTruthy();
    expect(screen.getByRole('heading', { level: 1 }).textContent).toBe('صحة الإشارة');
  });

  it('toggles to English and flips the document direction', () => {
    renderOperator();

    fireEvent.click(screen.getByRole('button', { name: 'English' }));

    expect(document.documentElement.dir).toBe('ltr');
    expect(document.documentElement.lang).toBe('en');
    expect(screen.getByRole('button', { name: 'العربية' })).toBeTruthy();
    expect(screen.getByRole('link', { name: 'Delivered ROAS' })).toBeTruthy();
    expect(screen.getByText('Choose a tenant')).toBeTruthy();
  });

  it('cycles the theme mode and sets data-theme', () => {
    render(<App />);

    fireEvent.click(screen.getByRole('button', { name: /المظهر: تلقائي/ }));
    expect(document.documentElement.getAttribute('data-theme')).toBe('light');
    fireEvent.click(screen.getByRole('button', { name: /المظهر: فاتح/ }));
    expect(document.documentElement.getAttribute('data-theme')).toBe('dark');
    expect(window.localStorage.getItem('motahai.theme')).toBe('dark');
  });

  it('prompts for a tenant on dashboard pages instead of calling the API', () => {
    window.location.hash = '#/roas';
    renderOperator();
    expect(screen.getByText('اختر متجرًا')).toBeTruthy();
  });

  it('shows the audiences page (tenant prompt) for its route', () => {
    window.location.hash = '#/audiences';
    renderOperator();
    expect(screen.getByRole('heading', { level: 1 }).textContent).toBe('الجماهير');
    expect(screen.getByText('اختر متجرًا')).toBeTruthy();
  });

  it('shows not-found for an unknown route', () => {
    window.location.hash = '#/nope';
    renderOperator();
    expect(screen.getByText('الصفحة غير موجودة')).toBeTruthy();
  });
});
