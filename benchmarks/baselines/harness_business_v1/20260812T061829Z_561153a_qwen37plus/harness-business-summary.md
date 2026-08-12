# PR8 Agent Harness 业务效果报告

> 该报告具备正式采样资格。 结果只描述固定 Dataset、模型、配置和匹配 Baseline；不代表线上用户成功率。

```json
{
  "by_difficulty": {
    "easy": {
      "direct_tool_without_resume": {
        "failure_attribution": {
          "assertion_failure": 6
        },
        "llm_call_count": 7,
        "p50_ms": 10881.057150001652,
        "p95_ms": 14185.699374998876,
        "sample_count": 6,
        "task_success_rate": 0.0,
        "task_successes": 0,
        "tool_call_count": 1
      },
      "full": {
        "failure_attribution": {
          "assertion_failure": 6,
          "judge_quality_failure": 9
        },
        "llm_call_count": 63,
        "p50_ms": 13453.893500001868,
        "p95_ms": 17457.176150001033,
        "sample_count": 36,
        "task_success_rate": 0.5833333333333334,
        "task_successes": 21,
        "tool_call_count": 39
      },
      "primary_capability_removed": {
        "failure_attribution": {
          "assertion_failure": 3,
          "judge_error": 1,
          "judge_quality_failure": 1
        },
        "llm_call_count": 6,
        "p50_ms": 8680.141700002423,
        "p95_ms": 11466.17687500202,
        "sample_count": 6,
        "task_success_rate": 0.16666666666666666,
        "task_successes": 1,
        "tool_call_count": 0
      },
      "recent_window_only": {
        "failure_attribution": {
          "assertion_failure": 3,
          "judge_quality_failure": 2
        },
        "llm_call_count": 6,
        "p50_ms": 12926.81894999987,
        "p95_ms": 21212.56962499956,
        "sample_count": 6,
        "task_success_rate": 0.16666666666666666,
        "task_successes": 1,
        "tool_call_count": 0
      },
      "single_agent_no_delegation": {
        "failure_attribution": {
          "judge_quality_failure": 3
        },
        "llm_call_count": 6,
        "p50_ms": 13715.991650000433,
        "p95_ms": 26851.97004999918,
        "sample_count": 6,
        "task_success_rate": 0.5,
        "task_successes": 3,
        "tool_call_count": 0
      },
      "single_agent_workspace": {
        "failure_attribution": {
          "assertion_failure": 3,
          "judge_quality_failure": 3
        },
        "llm_call_count": 12,
        "p50_ms": 13813.385800000106,
        "p95_ms": 14943.363225000212,
        "sample_count": 6,
        "task_success_rate": 0.0,
        "task_successes": 0,
        "tool_call_count": 6
      },
      "single_pass_research": {
        "failure_attribution": {
          "judge_quality_failure": 3
        },
        "llm_call_count": 6,
        "p50_ms": 9119.597650000287,
        "p95_ms": 10583.342799999627,
        "sample_count": 6,
        "task_success_rate": 0.5,
        "task_successes": 3,
        "tool_call_count": 0
      }
    },
    "hard": {
      "direct_tool_without_resume": {
        "failure_attribution": {
          "assertion_failure": 3,
          "judge_quality_failure": 4
        },
        "llm_call_count": 12,
        "p50_ms": 10149.745799997618,
        "p95_ms": 16492.563019999216,
        "sample_count": 9,
        "task_success_rate": 0.2222222222222222,
        "task_successes": 2,
        "tool_call_count": 3
      },
      "full": {
        "failure_attribution": {
          "assertion_failure": 8,
          "judge_quality_failure": 22
        },
        "llm_call_count": 106,
        "p50_ms": 18384.53285000105,
        "p95_ms": 38014.806189999574,
        "sample_count": 54,
        "task_success_rate": 0.4444444444444444,
        "task_successes": 24,
        "tool_call_count": 94
      },
      "primary_capability_removed": {
        "failure_attribution": {
          "assertion_failure": 9
        },
        "llm_call_count": 9,
        "p50_ms": 19613.839800000278,
        "p95_ms": 30033.002820000547,
        "sample_count": 9,
        "task_success_rate": 0.0,
        "task_successes": 0,
        "tool_call_count": 0
      },
      "recent_window_only": {
        "failure_attribution": {
          "assertion_failure": 6,
          "judge_quality_failure": 3
        },
        "llm_call_count": 9,
        "p50_ms": 20500.31680000029,
        "p95_ms": 30993.079679999937,
        "sample_count": 9,
        "task_success_rate": 0.0,
        "task_successes": 0,
        "tool_call_count": 0
      },
      "single_agent_no_delegation": {
        "failure_attribution": {
          "assertion_failure": 3,
          "judge_quality_failure": 2
        },
        "llm_call_count": 9,
        "p50_ms": 16673.838899998373,
        "p95_ms": 22758.752240000467,
        "sample_count": 9,
        "task_success_rate": 0.4444444444444444,
        "task_successes": 4,
        "tool_call_count": 0
      },
      "single_agent_workspace": {
        "failure_attribution": {
          "assertion_failure": 9
        },
        "llm_call_count": 18,
        "p50_ms": 17282.989299998007,
        "p95_ms": 37238.32093999881,
        "sample_count": 9,
        "task_success_rate": 0.0,
        "task_successes": 0,
        "tool_call_count": 13
      },
      "single_pass_research": {
        "failure_attribution": {
          "judge_quality_failure": 6
        },
        "llm_call_count": 9,
        "p50_ms": 19752.78249999974,
        "p95_ms": 36732.268939999616,
        "sample_count": 9,
        "task_success_rate": 0.3333333333333333,
        "task_successes": 3,
        "tool_call_count": 0
      }
    },
    "medium": {
      "direct_tool_without_resume": {
        "failure_attribution": {
          "assertion_failure": 12,
          "judge_quality_failure": 3
        },
        "llm_call_count": 24,
        "p50_ms": 15275.429400000576,
        "p95_ms": 18169.627679998666,
        "sample_count": 15,
        "task_success_rate": 0.0,
        "task_successes": 0,
        "tool_call_count": 9
      },
      "full": {
        "failure_attribution": {
          "assertion_failure": 6,
          "judge_error": 2,
          "judge_quality_failure": 22,
          "missing_final_delivery": 1
        },
        "llm_call_count": 189,
        "p50_ms": 17316.990750001423,
        "p95_ms": 27132.417260000148,
        "sample_count": 90,
        "task_success_rate": 0.6555555555555556,
        "task_successes": 59,
        "tool_call_count": 156
      },
      "primary_capability_removed": {
        "failure_attribution": {
          "assertion_failure": 12,
          "judge_quality_failure": 3
        },
        "llm_call_count": 15,
        "p50_ms": 15826.605599999311,
        "p95_ms": 29257.164170001488,
        "sample_count": 15,
        "task_success_rate": 0.0,
        "task_successes": 0,
        "tool_call_count": 0
      },
      "recent_window_only": {
        "failure_attribution": {
          "assertion_failure": 6,
          "judge_quality_failure": 9
        },
        "llm_call_count": 15,
        "p50_ms": 14926.81839999932,
        "p95_ms": 26573.178350000308,
        "sample_count": 15,
        "task_success_rate": 0.0,
        "task_successes": 0,
        "tool_call_count": 0
      },
      "single_agent_no_delegation": {
        "failure_attribution": {
          "judge_quality_failure": 9
        },
        "llm_call_count": 15,
        "p50_ms": 19049.05940000026,
        "p95_ms": 29717.25154999839,
        "sample_count": 15,
        "task_success_rate": 0.4,
        "task_successes": 6,
        "tool_call_count": 0
      },
      "single_agent_workspace": {
        "failure_attribution": {
          "assertion_failure": 15
        },
        "llm_call_count": 30,
        "p50_ms": 18417.893599998933,
        "p95_ms": 53333.825849999266,
        "sample_count": 15,
        "task_success_rate": 0.0,
        "task_successes": 0,
        "tool_call_count": 21
      },
      "single_pass_research": {
        "failure_attribution": {
          "judge_quality_failure": 10
        },
        "llm_call_count": 15,
        "p50_ms": 14475.790600001346,
        "p95_ms": 24625.72492000071,
        "sample_count": 15,
        "task_success_rate": 0.3333333333333333,
        "task_successes": 5,
        "tool_call_count": 0
      }
    }
  },
  "by_execution_condition": {
    "direct_tool_without_resume": {
      "direct_tool_without_resume": {
        "failure_attribution": {
          "assertion_failure": 21,
          "judge_quality_failure": 7
        },
        "llm_call_count": 43,
        "p50_ms": 12132.825750000848,
        "p95_ms": 17801.36812499768,
        "sample_count": 30,
        "task_success_rate": 0.06666666666666667,
        "task_successes": 2,
        "tool_call_count": 13
      }
    },
    "full": {
      "full": {
        "failure_attribution": {
          "assertion_failure": 20,
          "judge_error": 2,
          "judge_quality_failure": 53,
          "missing_final_delivery": 1
        },
        "llm_call_count": 358,
        "p50_ms": 16115.404649999618,
        "p95_ms": 28346.3337949981,
        "sample_count": 180,
        "task_success_rate": 0.5777777777777777,
        "task_successes": 104,
        "tool_call_count": 289
      }
    },
    "primary_capability_removed": {
      "primary_capability_removed": {
        "failure_attribution": {
          "assertion_failure": 24,
          "judge_error": 1,
          "judge_quality_failure": 4
        },
        "llm_call_count": 30,
        "p50_ms": 15694.874849999906,
        "p95_ms": 29711.900750001405,
        "sample_count": 30,
        "task_success_rate": 0.03333333333333333,
        "task_successes": 1,
        "tool_call_count": 0
      }
    },
    "recent_window_only": {
      "recent_window_only": {
        "failure_attribution": {
          "assertion_failure": 15,
          "judge_quality_failure": 14
        },
        "llm_call_count": 30,
        "p50_ms": 15028.30134999931,
        "p95_ms": 27781.14545999997,
        "sample_count": 30,
        "task_success_rate": 0.03333333333333333,
        "task_successes": 1,
        "tool_call_count": 0
      }
    },
    "single_agent_no_delegation": {
      "single_agent_no_delegation": {
        "failure_attribution": {
          "assertion_failure": 3,
          "judge_quality_failure": 14
        },
        "llm_call_count": 30,
        "p50_ms": 16791.674000000057,
        "p95_ms": 28981.30760999883,
        "sample_count": 30,
        "task_success_rate": 0.43333333333333335,
        "task_successes": 13,
        "tool_call_count": 0
      }
    },
    "single_agent_workspace": {
      "single_agent_workspace": {
        "failure_attribution": {
          "assertion_failure": 27,
          "judge_quality_failure": 3
        },
        "llm_call_count": 60,
        "p50_ms": 14814.361450000433,
        "p95_ms": 47333.42846999873,
        "sample_count": 30,
        "task_success_rate": 0.0,
        "task_successes": 0,
        "tool_call_count": 40
      }
    },
    "single_pass_research": {
      "single_pass_research": {
        "failure_attribution": {
          "judge_quality_failure": 19
        },
        "llm_call_count": 30,
        "p50_ms": 14481.517199999871,
        "p95_ms": 27692.317735000477,
        "sample_count": 30,
        "task_success_rate": 0.36666666666666664,
        "task_successes": 11,
        "tool_call_count": 0
      }
    }
  },
  "by_family": {
    "evidence_research": {
      "full": {
        "failure_attribution": {
          "judge_quality_failure": 9
        },
        "llm_call_count": 60,
        "p50_ms": 18267.463550000684,
        "p95_ms": 24035.038110001187,
        "sample_count": 30,
        "task_success_rate": 0.7,
        "task_successes": 21,
        "tool_call_count": 90
      },
      "single_pass_research": {
        "failure_attribution": {
          "judge_quality_failure": 19
        },
        "llm_call_count": 30,
        "p50_ms": 14481.517199999871,
        "p95_ms": 27692.317735000477,
        "sample_count": 30,
        "task_success_rate": 0.36666666666666664,
        "task_successes": 11,
        "tool_call_count": 0
      }
    },
    "long_context_continuity": {
      "full": {
        "failure_attribution": {
          "judge_quality_failure": 10
        },
        "llm_call_count": 45,
        "p50_ms": 17197.796299999027,
        "p95_ms": 33516.89557499846,
        "sample_count": 30,
        "task_success_rate": 0.6666666666666666,
        "task_successes": 20,
        "tool_call_count": 15
      },
      "recent_window_only": {
        "failure_attribution": {
          "assertion_failure": 15,
          "judge_quality_failure": 14
        },
        "llm_call_count": 30,
        "p50_ms": 15028.30134999931,
        "p95_ms": 27781.14545999997,
        "sample_count": 30,
        "task_success_rate": 0.03333333333333333,
        "task_successes": 1,
        "tool_call_count": 0
      }
    },
    "mixed_complex": {
      "full": {
        "failure_attribution": {
          "assertion_failure": 5,
          "judge_quality_failure": 11
        },
        "llm_call_count": 70,
        "p50_ms": 20680.110799998147,
        "p95_ms": 37455.23904500112,
        "sample_count": 30,
        "task_success_rate": 0.4666666666666667,
        "task_successes": 14,
        "tool_call_count": 68
      },
      "primary_capability_removed": {
        "failure_attribution": {
          "assertion_failure": 24,
          "judge_error": 1,
          "judge_quality_failure": 4
        },
        "llm_call_count": 30,
        "p50_ms": 15694.874849999906,
        "p95_ms": 29711.900750001405,
        "sample_count": 30,
        "task_success_rate": 0.03333333333333333,
        "task_successes": 1,
        "tool_call_count": 0
      }
    },
    "multi_agent": {
      "full": {
        "failure_attribution": {
          "judge_quality_failure": 7
        },
        "llm_call_count": 30,
        "p50_ms": 16691.604149998966,
        "p95_ms": 26733.08902999961,
        "sample_count": 30,
        "task_success_rate": 0.7666666666666667,
        "task_successes": 23,
        "tool_call_count": 0
      },
      "single_agent_no_delegation": {
        "failure_attribution": {
          "assertion_failure": 3,
          "judge_quality_failure": 14
        },
        "llm_call_count": 30,
        "p50_ms": 16791.674000000057,
        "p95_ms": 28981.30760999883,
        "sample_count": 30,
        "task_success_rate": 0.43333333333333335,
        "task_successes": 13,
        "tool_call_count": 0
      }
    },
    "tool_approval_workflow": {
      "direct_tool_without_resume": {
        "failure_attribution": {
          "assertion_failure": 21,
          "judge_quality_failure": 7
        },
        "llm_call_count": 43,
        "p50_ms": 12132.825750000848,
        "p95_ms": 17801.36812499768,
        "sample_count": 30,
        "task_success_rate": 0.06666666666666667,
        "task_successes": 2,
        "tool_call_count": 13
      },
      "full": {
        "failure_attribution": {
          "assertion_failure": 6,
          "judge_error": 1,
          "judge_quality_failure": 9,
          "missing_final_delivery": 1
        },
        "llm_call_count": 51,
        "p50_ms": 10838.067199998477,
        "p95_ms": 26378.258050000473,
        "sample_count": 30,
        "task_success_rate": 0.43333333333333335,
        "task_successes": 13,
        "tool_call_count": 36
      }
    },
    "workspace_engineering": {
      "full": {
        "failure_attribution": {
          "assertion_failure": 9,
          "judge_error": 1,
          "judge_quality_failure": 7
        },
        "llm_call_count": 102,
        "p50_ms": 17254.869550000876,
        "p95_ms": 25068.393464998553,
        "sample_count": 30,
        "task_success_rate": 0.43333333333333335,
        "task_successes": 13,
        "tool_call_count": 80
      },
      "single_agent_workspace": {
        "failure_attribution": {
          "assertion_failure": 27,
          "judge_quality_failure": 3
        },
        "llm_call_count": 60,
        "p50_ms": 14814.361450000433,
        "p95_ms": 47333.42846999873,
        "sample_count": 30,
        "task_success_rate": 0.0,
        "task_successes": 0,
        "tool_call_count": 40
      }
    }
  },
  "candidate_condition": {
    "model": "qwen3.7-plus",
    "provider": "qwen",
    "temperature": null
  },
  "config_hash": "3d8c8698855dacbb",
  "dataset": "agent_harness_business_v1",
  "dataset_content_hash": "d9ff26ed60700a4d9595e46fd622bfa7c2befd6dd1804ce7548df2683c71aa8a",
  "dataset_version": "1",
  "delivery_quality": {
    "by_execution_condition": {
      "direct_tool_without_resume": {
        "constraint_violation_rate": 0.3333333333333333,
        "dimensions": {
          "actionability": {
            "failed": 3,
            "observations": 9,
            "pass_rate": 0.6666666666666666,
            "passed": 6
          },
          "completeness": {
            "failed": 0,
            "observations": 0,
            "pass_rate": null,
            "passed": 0
          },
          "constraint_compliance": {
            "failed": 3,
            "observations": 9,
            "pass_rate": 0.6666666666666666,
            "passed": 6
          },
          "groundedness": {
            "failed": 7,
            "observations": 18,
            "pass_rate": 0.6111111111111112,
            "passed": 11
          }
        },
        "entered_judge": 9,
        "fact_overreach_rate": 0.4444444444444444
      },
      "full": {
        "constraint_violation_rate": 0.050955414012738856,
        "dimensions": {
          "actionability": {
            "failed": 9,
            "observations": 58,
            "pass_rate": 0.8448275862068966,
            "passed": 49
          },
          "completeness": {
            "failed": 4,
            "observations": 108,
            "pass_rate": 0.9629629629629629,
            "passed": 104
          },
          "constraint_compliance": {
            "failed": 8,
            "observations": 157,
            "pass_rate": 0.9490445859872612,
            "passed": 149
          },
          "groundedness": {
            "failed": 49,
            "observations": 305,
            "pass_rate": 0.839344262295082,
            "passed": 256
          }
        },
        "entered_judge": 157,
        "fact_overreach_rate": 0.2611464968152866
      },
      "primary_capability_removed": {
        "constraint_violation_rate": 0.0,
        "dimensions": {
          "actionability": {
            "failed": 1,
            "observations": 5,
            "pass_rate": 0.8,
            "passed": 4
          },
          "completeness": {
            "failed": 0,
            "observations": 2,
            "pass_rate": 1.0,
            "passed": 2
          },
          "constraint_compliance": {
            "failed": 0,
            "observations": 5,
            "pass_rate": 1.0,
            "passed": 5
          },
          "groundedness": {
            "failed": 3,
            "observations": 8,
            "pass_rate": 0.625,
            "passed": 5
          }
        },
        "entered_judge": 5,
        "fact_overreach_rate": 0.6
      },
      "recent_window_only": {
        "constraint_violation_rate": 0.09523809523809523,
        "dimensions": {
          "actionability": {
            "failed": 3,
            "observations": 3,
            "pass_rate": 0.0,
            "passed": 0
          },
          "completeness": {
            "failed": 4,
            "observations": 12,
            "pass_rate": 0.6666666666666666,
            "passed": 8
          },
          "constraint_compliance": {
            "failed": 2,
            "observations": 21,
            "pass_rate": 0.9047619047619048,
            "passed": 19
          },
          "groundedness": {
            "failed": 16,
            "observations": 24,
            "pass_rate": 0.3333333333333333,
            "passed": 8
          }
        },
        "entered_judge": 15,
        "fact_overreach_rate": 0.6666666666666666
      },
      "single_agent_no_delegation": {
        "constraint_violation_rate": 0.0,
        "dimensions": {
          "actionability": {
            "failed": 2,
            "observations": 9,
            "pass_rate": 0.7777777777777778,
            "passed": 7
          },
          "completeness": {
            "failed": 0,
            "observations": 21,
            "pass_rate": 1.0,
            "passed": 21
          },
          "constraint_compliance": {
            "failed": 0,
            "observations": 24,
            "pass_rate": 1.0,
            "passed": 24
          },
          "groundedness": {
            "failed": 14,
            "observations": 54,
            "pass_rate": 0.7407407407407407,
            "passed": 40
          }
        },
        "entered_judge": 27,
        "fact_overreach_rate": 0.48148148148148145
      },
      "single_agent_workspace": {
        "constraint_violation_rate": 0.0,
        "dimensions": {
          "actionability": {
            "failed": 3,
            "observations": 3,
            "pass_rate": 0.0,
            "passed": 0
          },
          "completeness": {
            "failed": 0,
            "observations": 0,
            "pass_rate": null,
            "passed": 0
          },
          "constraint_compliance": {
            "failed": 0,
            "observations": 3,
            "pass_rate": 1.0,
            "passed": 3
          },
          "groundedness": {
            "failed": 2,
            "observations": 6,
            "pass_rate": 0.6666666666666666,
            "passed": 4
          }
        },
        "entered_judge": 3,
        "fact_overreach_rate": 0.6666666666666666
      },
      "single_pass_research": {
        "constraint_violation_rate": 0.0,
        "dimensions": {
          "actionability": {
            "failed": 6,
            "observations": 21,
            "pass_rate": 0.7142857142857143,
            "passed": 15
          },
          "completeness": {
            "failed": 0,
            "observations": 12,
            "pass_rate": 1.0,
            "passed": 12
          },
          "constraint_compliance": {
            "failed": 0,
            "observations": 27,
            "pass_rate": 1.0,
            "passed": 27
          },
          "groundedness": {
            "failed": 16,
            "observations": 60,
            "pass_rate": 0.7333333333333333,
            "passed": 44
          }
        },
        "entered_judge": 30,
        "fact_overreach_rate": 0.5333333333333333
      }
    },
    "constraint_violation_rate": 0.052845528455284556,
    "dimensions": {
      "actionability": {
        "failed": 27,
        "observations": 108,
        "pass_rate": 0.75,
        "passed": 81
      },
      "completeness": {
        "failed": 8,
        "observations": 155,
        "pass_rate": 0.9483870967741935,
        "passed": 147
      },
      "constraint_compliance": {
        "failed": 13,
        "observations": 246,
        "pass_rate": 0.9471544715447154,
        "passed": 233
      },
      "groundedness": {
        "failed": 107,
        "observations": 475,
        "pass_rate": 0.7747368421052632,
        "passed": 368
      }
    },
    "entered_judge": 246,
    "fact_overreach_rate": 0.3617886178861789
  },
  "formal_repeat": 3,
  "formal_sampling": true,
  "full": {
    "failure_attribution": {
      "assertion_failure": 20,
      "judge_error": 2,
      "judge_quality_failure": 53,
      "missing_final_delivery": 1
    },
    "llm_call_count": 358,
    "p50_ms": 16115.404649999618,
    "p95_ms": 28346.3337949981,
    "sample_count": 180,
    "task_success_rate": 0.5777777777777777,
    "task_successes": 104,
    "tool_call_count": 289
  },
  "git_commit": "561153a",
  "harness_value": {
    "baseline_success_rate": 0.15555555555555556,
    "cluster_bootstrap_95_percentage_points": [
      31.666666666666664,
      53.333333333333336
    ],
    "full_success_rate": 0.5777777777777777,
    "lift_percentage_points": 42.222222222222214
  },
  "instance_count": 60,
  "judge_condition": {
    "model": "qwen3.7-plus",
    "provider": "qwen"
  },
  "matched_baseline": {
    "failure_attribution": {
      "assertion_failure": 90,
      "judge_error": 1,
      "judge_quality_failure": 61
    },
    "llm_call_count": 223,
    "p50_ms": 14925.652149999223,
    "p95_ms": 30324.197780000206,
    "sample_count": 180,
    "task_success_rate": 0.15555555555555556,
    "task_successes": 28,
    "tool_call_count": 53
  },
  "qualification": "formal",
  "sample_count": 360,
  "workflow_version": "2"
}
```
