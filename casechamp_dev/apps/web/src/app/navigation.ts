import { useLocation, useNavigate } from 'react-router-dom';
import type { NavigateOptions } from 'react-router-dom';

export function useScreenNavigation() {
  const navigate = useNavigate();
  const { pathname } = useLocation();
  return (to: string, options?: NavigateOptions) => {
    if (window.location.pathname === pathname) navigate(to, options);
  };
}
