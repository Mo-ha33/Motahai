import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import App from './App';

describe('App shell', () => {
  beforeEach(() => {
    window.localStorage.clear();
    window.location.hash = '';
    document.documentElement.lang = '';
    document.documentElement.dir = '';
  });

  afterEach(() => {
    cleanup();
  });

  it('renders Arabic by default with an RTL document', () => {
    render(<App />);

    expect(document.documentElement.dir).toBe('rtl');
    expect(document.documentElement.lang).toBe('ar');
    expect(screen.getByRole('button', { name: 'English' })).toBeTruthy();
    expect(screen.getByRole('heading', { level: 1 }).textContent).toBe('صحة الإشارة');
  });

  it('toggles to English and flips the document direction', () => {
    render(<App />);

    fireEvent.click(screen.getByRole('button', { name: 'English' }));

    expect(document.documentElement.dir).toBe('ltr');
    expect(document.documentElement.lang).toBe('en');
    expect(screen.getByRole('button', { name: 'العربية' })).toBeTruthy();
    expect(screen.getByRole('link', { name: 'Delivered ROAS' })).toBeTruthy();
    expect(screen.getByText('No data yet')).toBeTruthy();
  });

  it('shows the empty state for a known route and not-found for an unknown one', () => {
    window.location.hash = '#/audiences';
    const { unmount } = render(<App />);
    expect(screen.getByRole('heading', { level: 1 }).textContent).toBe('الجماهير');
    expect(screen.getByText('لا توجد بيانات بعد')).toBeTruthy();
    unmount();

    window.location.hash = '#/nope';
    render(<App />);
    expect(screen.getByText('الصفحة غير موجودة')).toBeTruthy();
  });
});
