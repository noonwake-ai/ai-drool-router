import { defineConfig } from 'vite';
export default defineConfig({server:{proxy:{'/api':'http://127.0.0.1:4191','/artifacts':'http://127.0.0.1:4191'}},build:{target:'es2022'}});
