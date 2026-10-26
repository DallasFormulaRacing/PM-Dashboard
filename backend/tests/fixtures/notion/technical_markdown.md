# Synthetic calibration guide

These procedures and values are fictional test fixtures, not real engineering guidance.

## Preparation

The synthetic operator begins by checking the fixture identifier against the reference log. The fixture identifier is TEST-42, and the synthetic revision label is REV-C. These labels must remain intact when the document is split into chunks. They let a retrieval test distinguish a precise reference from a general discussion of calibration. The setup notes also record that this page belongs to a fictional documentation collection used only for software tests.

Before collecting the fictional readings, the operator records the synthetic device name and the fixture revision. Each record includes a short description of the test purpose. The description explains which fixture behavior is being checked and which result identifiers should appear in the output. A later reviewer should be able to connect the readings to the matching procedure without depending on the position of a paragraph inside this page.

## Procedure

The fictional calibration sequence has three stages: setup, capture, and review. During setup, the synthetic operator associates the fixture identifier with the result log. During capture, the operator records the sample label and its corresponding synthetic value. During review, the operator checks that the values appear in the expected order and that no row has disappeared from the fixture. These are requirements for testing document processing, not instructions for operating physical equipment.

The capture stage preserves a complete record for every fictional sample. A record includes its identifier, its description, and the reason it exists in the fixture. The identifier must not be separated from the description when the record appears in a table. Keeping the table header beside every group of rows gives each retrieved passage enough context to explain the values without reading a different chunk first.

The review stage compares the processed content with the original synthetic source. If a paragraph has to be split because it exceeds the token budget, neighboring fragments can share a small amount of prose. This overlap helps retain the immediate context around a sentence boundary. The overlap should not be added between unrelated headings, copied across separate pages, or used to repeat an entire checklist in every fragment. Each heading continues to identify its own coherent section of the procedure.

The synthetic reviewer checks the preservation of unusual characters as well as ordinary English words. Fixture examples can include calibration labels such as 校准, temperature labels such as 温度, and symbols such as 🚗 and 🧪. The processing code must preserve these characters without introducing replacement symbols. A split at a character boundary can retain valid text even when a tokenizer represents one displayed character with several underlying tokens.

The final capture record is named CAPTURE-END. This identifier appears at the end of the long procedure section to help tests check that the final fragment was retained. A processing failure must not produce an apparently successful document containing only the beginning of this section. The complete set of chunks should still contain the setup explanation, capture explanation, review explanation, unusual-character examples, and this closing identifier.

## Reference results

| Sample | Synthetic value | Interpretation |
| --- | --- | --- |
| SAMPLE-A | 10 | Fixture baseline |
| SAMPLE-B | 20 | Fixture comparison |
| SAMPLE-C | 30 | Fixture final value |

## Example code

```python
# This is literal fixture code, not a document heading.
sample_id = "TEST-42"
print(sample_id)
```
