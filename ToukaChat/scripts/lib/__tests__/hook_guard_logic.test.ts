import { describe, it, expect } from 'vitest';
import { Project } from 'ts-morph';
import { checkHookViolations } from '../hook_guard_logic';

describe('Hook Sovereignty Guard Logic', () => {
  const project = new Project({ useInMemoryFileSystem: true });

  it('should detect fetch() in components', () => {
    const code = `
      export const MyComponent = () => {
        const load = () => fetch('/api/data');
        return <div>Test</div>;
      };
    `;
    const sourceFile = project.createSourceFile('src/components/MyComponent.tsx', code);
    const violations = checkHookViolations(sourceFile);
    
    expect(violations.length).toBe(1);
    expect(violations[0].message).toContain('fetch()');
  });

  it('should detect axios in components', () => {
    const code = `
      import axios from 'axios';
      export const MyComponent = () => {
        axios.get('/api/data');
        return <div>Test</div>;
      };
    `;
    const sourceFile = project.createSourceFile('src/components/AxiosComponent.tsx', code);
    const violations = checkHookViolations(sourceFile);
    
    expect(violations.length).toBe(1);
    expect(violations[0].message).toContain('axios');
  });

  it('should detect localStorage in components', () => {
    const code = `
      export const MyComponent = () => {
        const val = localStorage.getItem('token');
        return <div>{val}</div>;
      };
    `;
    const sourceFile = project.createSourceFile('src/components/StorageComponent.tsx', code);
    const violations = checkHookViolations(sourceFile);
    
    expect(violations.length).toBe(1);
    expect(violations[0].message).toContain('localStorage');
  });

  it('should allow fetch in hooks directory', () => {
    const code = `
      export const useData = () => {
        return fetch('/api/data');
      };
    `;
    const sourceFile = project.createSourceFile('src/hooks/useData.ts', code);
    const violations = checkHookViolations(sourceFile);
    
    expect(violations.length).toBe(0);
  });

  it('should allow custom hooks in components', () => {
    const code = `
      import { useAgentSettings } from '../hooks/useAgentSettings';
      export const MyComponent = () => {
        const settings = useAgentSettings();
        return <div>Test</div>;
      };
    `;
    const sourceFile = project.createSourceFile('src/components/CleanComponent.tsx', code);
    const violations = checkHookViolations(sourceFile);
    
    expect(violations.length).toBe(0);
  });
});
