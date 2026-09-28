import React,{createContext,useCallback,useContext,useEffect,useMemo,useState} from 'react';
import {LANGUAGES,detectLanguage,setActiveLanguage,translate} from './i18n-core.js';

export {LANGUAGES,detectLanguage,translate,tr,currentLanguage,setActiveLanguage} from './i18n-core.js';

const LanguageContext=createContext(null);

export function LanguageProvider({children}){
  const [language,setLanguage]=useState(detectLanguage);
  useEffect(()=>{
    setActiveLanguage(language);
    document.documentElement.lang=language==='zh'?'zh-CN':'en';
    try{window.localStorage.setItem('drool-detector-language',language);}catch{/* storage can be blocked */}
  },[language]);
  const t=useCallback((key,vars)=>translate(language,key,vars),[language]);
  const value=useMemo(()=>({language,setLanguage,t}),[language,t]);
  return React.createElement(LanguageContext.Provider,{value},children);
}

export function useI18n(){
  const value=useContext(LanguageContext);
  if(value)return value;
  return {language:'zh',setLanguage(){},t:(key,vars)=>translate('zh',key,vars)};
}
