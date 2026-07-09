class DataFrame:
    """A custom class which emulates pandas DataFrame for limited functionality.

    Takes a list of dictionaries as input. Each element in the list corresponds
    to a row, and each key in the dictionary corresponds to a column.
    """

    def __init__(self, content_dict, primary_key, sep=","):
        self.content_dict = content_dict
        self.primary_key = primary_key
        self.sep = sep
        self.content = self.parse_dict(content_dict)
        self.indices = self.build_key_indices()

    def parse_dict(self, content):
        # Get a list of all unique keys
        columns = set()
        for row in content:
            columns = columns.union(set(list(row.keys())))
        columns = list(columns)

        content_string = [self.sep.join(columns)]
        for row in content:
            row_content = []
            for key in columns:
                element = str(row.get(key, ""))
                row_content.append(element)
            parsed_row = self.sep.join(row_content)
            content_string.append(parsed_row)
        content_string = "\n".join(content_string)
        return content_string

    def build_key_indices(self):
        indices = {row[self.primary_key]: i for i, row in enumerate(self.content_dict)}
        return indices

    def to_csv(self, filepath, *args, **kwargs):
        """Writes the contents to a CSV file.

        Args:
            filepath: Path to the CSV file.
            *args, **kwargs: accepted for compatibility with the pandas API
                (e.g. header=, index=) but otherwise unused.
        """
        with open(filepath, "w") as f:
            f.write(self.content)

    def __len__(self):
        return len(self.content_dict)

    def retrieve(self, key_value, field):
        index = self.indices[key_value]
        return self.content_dict[index].get(field, "")


def read_csv(path, sep=","):
    """NOT PORTED: the body of this function was cut off in the source paste
    (it stops after building `cols` from a naive `row.split(sep)`, with no
    handling shown for quoted fields containing the separator, and never
    reaches a return statement). Nothing in metashape_pipeline.py calls
    read_csv — the pipeline only ever writes CSVs via DataFrame.to_csv(),
    never reads them back — so rather than guess at the missing body, this
    raises until someone pastes the real implementation.
    """
    raise NotImplementedError(
        "read_csv was not fully captured from source; paste the real "
        "src/tasks/auto_sfm/dataframe.py read_csv() body to restore it."
    )