"use client";

import { useState, useEffect, useRef } from "react";

export interface StepConfig {
  label: string;
  subMessages: string[];
}

const VIDEO_STEPS: StepConfig[] = [
  { label: "Fetching transcript", subMessages: ["Downloading captions...", "Processing text..."] },
  { label: "Analyzing title vs content", subMessages: ["Reading through the content...", "Comparing with title claims..."] },
  { label: "Evaluating focus and deception", subMessages: ["Measuring time on topic...", "Checking for misleading claims..."] },
  { label: "Computing score", subMessages: ["Calculating weighted score...", "Applying penalties..."] },
];

export { VIDEO_STEPS };

export function stageToStep(stage: string, pollCount: number): number {
  if (stage === "fetching_transcript") return 0;
  if (stage === "analyzing" && pollCount < 3) return 1;
  if (stage === "analyzing" && pollCount < 6) return 2;
  if (stage === "analyzing" || stage === "computing") return 3;
  return -1;
}

export default function EvalStepper({ currentStep, steps, activeSubMessage }: { currentStep: number; steps?: StepConfig[]; activeSubMessage?: string }) {
  const stepsToUse = steps || VIDEO_STEPS;
  const [displayStep, setDisplayStep] = useState(0);
  const [subIdx, setSubIdx] = useState(0);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const subRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const prevStep = useRef(currentStep);

  // Time-based advancement: step 0 -> 1 after 4s
  useEffect(() => {
    if (currentStep === 0 && displayStep === 0) {
      timerRef.current = setTimeout(() => {
        setDisplayStep(1);
        setSubIdx(0);
      }, 4000);
    }
    return () => { if (timerRef.current) clearTimeout(timerRef.current); };
  }, [currentStep, displayStep]);

  // When API advances past our display, sync up
  useEffect(() => {
    if (currentStep > displayStep) {
      setDisplayStep(currentStep);
      setSubIdx(0);
    }
    prevStep.current = currentStep;
  }, [currentStep, displayStep]);

  // Rotate sub-messages
  useEffect(() => {
    if (subRef.current) clearInterval(subRef.current);
    subRef.current = setInterval(() => {
      setSubIdx(prev => prev + 1);
    }, 3000);
    return () => { if (subRef.current) clearInterval(subRef.current); };
  }, [displayStep]);

  return (
    <div className="eval-stepper">
      {stepsToUse.map((step, i) => {
        const isDone = i < displayStep;
        const isActive = i === displayStep;
        const isPending = i > displayStep;

        return (
          <div
            key={step.label}
            className={`eval-step ${isDone ? "eval-step-done" : ""} ${isActive ? "eval-step-active" : ""} ${isPending ? "eval-step-pending" : ""}`}
          >
            <div className="eval-step-indicator">
              {isDone ? (
                <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
                  <path d="M2.5 7L5.5 10L11.5 4" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
                </svg>
              ) : isActive ? (
                <div className="eval-step-spinner" />
              ) : (
                <div className="eval-step-dot" />
              )}
            </div>
            <div className="eval-step-content">
              <span className="eval-step-label">{step.label}</span>
              {isActive && (
                <span className="eval-step-sub">
                  {activeSubMessage || step.subMessages[subIdx % step.subMessages.length]}
                </span>
              )}
            </div>
          </div>
        );
      })}
    </div>
  );
}
